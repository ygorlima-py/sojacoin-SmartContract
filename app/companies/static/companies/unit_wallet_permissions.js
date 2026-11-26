"use strict";

const permissionSection = document.querySelector("[data-unit-wallet-permissions]");

function permissionCsrfToken() {
  const input = permissionSection?.querySelector('input[name="csrfmiddlewaretoken"]');
  if (input?.value) return input.value;

  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("csrftoken="));
  return cookie ? decodeURIComponent(cookie.slice("csrftoken=".length)) : "";
}

async function postPermissionForm(url, values = {}) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      "X-CSRFToken": permissionCsrfToken(),
      Accept: "application/json",
    },
    body: new URLSearchParams(values),
  });

  let result = {};
  try {
    result = await response.json();
  } catch (_error) {
    // The message area still reports a readable fallback for unexpected responses.
  }
  if (!response.ok && response.status !== 202) {
    throw new Error(result.error || gettext("O servidor não conseguiu preparar esta alteração."));
  }
  return result;
}

function showPermissionMessage(message) {
  const target = permissionSection?.querySelector("[data-wallet-permission-message]");
  if (target) target.textContent = gettext(message);
}

function setPermissionButtonBusy(button, busy) {
  button.disabled = busy;
  button.classList.toggle("opacity-60", busy);
  button.classList.toggle("cursor-not-allowed", busy);
}

function permissionHashKey(attemptId) {
  return `sojacoin-unit-wallet-permission-hash-${attemptId}`;
}

function rememberPermissionHash(attemptId, hash) {
  try {
    window.sessionStorage.setItem(permissionHashKey(attemptId), hash);
  } catch (_error) {
    // The operator can paste the same hash manually if session storage is unavailable.
  }
}

function restorePermissionHashes() {
  permissionSection
    ?.querySelectorAll("[data-wallet-permission-hash-input]")
    .forEach((input) => {
      try {
        input.value =
          window.sessionStorage.getItem(
            permissionHashKey(input.dataset.walletPermissionHashInput),
          ) || "";
      } catch (_error) {
        // The hash field remains available for manual recovery.
      }
    });
}

async function registerPermissionHash(input, button) {
  const attemptId = input.dataset.walletPermissionHashInput;
  const transactionHash = input.value.trim();
  if (!transactionHash) {
    showPermissionMessage(gettext("Informe a hash retornada pela carteira dona."));
    return;
  }

  rememberPermissionHash(attemptId, transactionHash);
  setPermissionButtonBusy(button, true);
  showPermissionMessage(gettext("Conferindo a transação no RPC…"));
  try {
    const result = await postPermissionForm(input.dataset.walletPermissionHashUrl, {
      transaction_hash: transactionHash,
    });
    showPermissionMessage(result.message);
    if (result.status === "waiting_for_rpc") {
      setPermissionButtonBusy(button, false);
      return;
    }
    window.location.reload();
  } catch (error) {
    showPermissionMessage(`${error.message} Tente novamente com esta mesma hash.`);
    setPermissionButtonBusy(button, false);
  }
}

async function confirmWalletPermission(button) {
  setPermissionButtonBusy(button, true);
  showPermissionMessage(gettext("Consultando a confirmação da rede…"));
  try {
    const result = await postPermissionForm(
      button.dataset.walletPermissionConfirmUrl,
    );
    showPermissionMessage(result.message);
    if (result.status === "confirmed" || result.status === "failed") {
      window.location.reload();
      return;
    }
  } catch (error) {
    showPermissionMessage(error.message);
  }
  setPermissionButtonBusy(button, false);
}

async function sendPermissionChange(button) {
  if (!window.ethereum) {
    showPermissionMessage(gettext("Carteira compatível não encontrada neste navegador."));
    return;
  }

  const select = button.dataset.walletPermissionSelectId
    ? document.getElementById(button.dataset.walletPermissionSelectId)
    : null;
  const target = button.dataset.walletTarget || select?.value;
  if (!target) {
    showPermissionMessage(gettext("Selecione a pessoa e a carteira que receberão a permissão."));
    return;
  }

  setPermissionButtonBusy(button, true);
  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    if (!accounts?.length) throw new Error(gettext("Conecte a carteira dona do UnitRegistry."));

    const ownerAddress = accounts[0];
    const chainHex = await window.ethereum.request({ method: "eth_chainId" });
    const chainId = Number.parseInt(chainHex, 16);
    const expectedChainId = Number(button.dataset.chainId);
    if (chainId !== expectedChainId) {
      throw new Error(
        interpolate(
          gettext("A carteira está na rede %(network)s. Troque para %(chainId)s."),
          { network: chainId || gettext("desconhecida"), chainId: expectedChainId },
          true,
        ),
      );
    }

    showPermissionMessage(gettext("Conferindo a unidade e preparando a solicitação…"));
    const prepared = await postPermissionForm(button.dataset.walletPrepareUrl, {
      target,
      owner_address: ownerAddress,
      wallet_chain_id: String(chainId),
    });

    // An open or submitted attempt is recovered from its saved payload and never resent here.
    if (prepared.status === "submitted" || prepared.status === "recover") {
      showPermissionMessage(
        gettext("Já existe uma solicitação aberta para esta carteira. Confira a hash ou a confirmação na página."),
      );
      window.location.reload();
      return;
    }
    if (prepared.owner_address.toLowerCase() !== ownerAddress.toLowerCase()) {
      throw new Error(gettext("Conecte a carteira dona do UnitRegistry."));
    }

    await postPermissionForm(prepared.started_url);
    showPermissionMessage(gettext("Confirme a alteração de permissão na carteira dona."));

    let transactionHash;
    try {
      transactionHash = await window.ethereum.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: ownerAddress,
            to: prepared.contract_address,
            data: prepared.transaction_data,
            value: "0x0",
          },
        ],
      });
    } catch (error) {
      if (error?.code === 4001) {
        await postPermissionForm(prepared.rejected_url);
        showPermissionMessage(gettext("A solicitação foi recusada. Você pode tentar novamente."));
        window.location.reload();
        return;
      }

      showPermissionMessage(
        gettext("Não foi possível saber se a carteira enviou a transação. Recarregue e informe a hash nesta tentativa; não inicie outro envio."),
      );
      window.location.reload();
      return;
    }

    rememberPermissionHash(prepared.attempt_id, transactionHash);
    showPermissionMessage(gettext("Hash recebida. Conferindo a transação na rede…"));
    try {
      const result = await postPermissionForm(prepared.register_url, {
        transaction_hash: transactionHash,
      });
      showPermissionMessage(result.message);
    } catch (error) {
      // Reload the saved attempt so the returned hash can be retried safely.
      showPermissionMessage(`${error.message} A hash foi guardada para conferência novamente.`);
    }
    window.location.reload();
  } catch (error) {
    showPermissionMessage(error.message);
    setPermissionButtonBusy(button, false);
  }
}

if (permissionSection) {
  restorePermissionHashes();
  permissionSection.addEventListener("click", (event) => {
    const actionButton = event.target.closest("[data-wallet-permission-action]");
    if (actionButton) {
      sendPermissionChange(actionButton);
      return;
    }

    const hashButton = event.target.closest("[data-wallet-permission-register-hash]");
    if (hashButton) {
      const input = hashButton
        .closest(".flex")
        ?.querySelector("[data-wallet-permission-hash-input]");
      if (input) registerPermissionHash(input, hashButton);
      return;
    }

    const confirmButton = event.target.closest(
      "[data-wallet-permission-confirm-url]",
    );
    if (confirmButton) confirmWalletPermission(confirmButton);
  });
}
