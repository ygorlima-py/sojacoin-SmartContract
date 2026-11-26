"use strict";

const registrationPanel = document.getElementById("unit-blockchain-registration");

function csrfToken() {
  const formToken = document.querySelector('input[name="csrfmiddlewaretoken"]');
  if (formToken?.value) return formToken.value;

  const cookie = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("csrftoken="));
  return cookie ? decodeURIComponent(cookie.slice("csrftoken=".length)) : "";
}

async function postForm(url, values = {}) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      "X-CSRFToken": csrfToken(),
      Accept: "application/json",
    },
    body: new URLSearchParams(values),
  });

  let result = {};
  try {
    result = await response.json();
  } catch (_error) {
    // Keep a useful message if an unexpected HTML response reaches this endpoint.
  }
  if (!response.ok && response.status !== 202) {
    throw new Error(result.error || gettext("O servidor não conseguiu concluir esta etapa."));
  }
  return { ...result, httpStatus: response.status };
}

function showMessage(message) {
  const target = registrationPanel?.querySelector("[data-unit-message]");
  if (target) target.textContent = gettext(message);
}

function hashStorageKey(attemptId) {
  return `sojacoin-unit-registration-hash-${attemptId}`;
}

function rejectionStorageKey(attemptId) {
  return `sojacoin-unit-registration-rejected-${attemptId}`;
}

function rememberHash(attemptId, hash) {
  try {
    window.sessionStorage.setItem(hashStorageKey(attemptId), hash);
  } catch (_error) {
    // The hash remains visible in the current page if browser storage is disabled.
  }
}

function clearHash(attemptId) {
  try {
    window.sessionStorage.removeItem(hashStorageKey(attemptId));
  } catch (_error) {
    // Server records remain authoritative even when session storage is unavailable.
  }
}

function rememberWalletRejection(attemptId, rejectedUrl) {
  try {
    window.sessionStorage.setItem(
      rejectionStorageKey(attemptId),
      JSON.stringify({ rejectedUrl }),
    );
  } catch (_error) {
    // The current page still offers a retry button if the server request fails.
  }
}

function clearWalletRejection(attemptId) {
  try {
    window.sessionStorage.removeItem(rejectionStorageKey(attemptId));
  } catch (_error) {
    // A stale retry flag is harmless; the server validates whether the attempt is open.
  }
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle("opacity-60", busy);
  button.classList.toggle("cursor-not-allowed", busy);
}

async function recordWalletRejection(attemptId, rejectedUrl, button = null) {
  setBusy(button, true);
  try {
    await postForm(rejectedUrl);
    clearWalletRejection(attemptId);
    window.location.reload();
  } catch (error) {
    rememberWalletRejection(attemptId, rejectedUrl);
    showMessage(
      interpolate(
        gettext("A carteira recusou o pedido, mas o servidor ainda não registrou a resposta. Use “Registrar recusa” para repetir essa atualização. %(error)s"),
        { error: error.message },
        true,
      ),
    );
    setBusy(button, false);
  }
}

function readWalletRejection(attemptId) {
  try {
    const stored = window.sessionStorage.getItem(rejectionStorageKey(attemptId));
    return stored ? JSON.parse(stored) : null;
  } catch (_error) {
    return null;
  }
}

function restoreAttemptRecovery() {
  const input = registrationPanel?.querySelector("[data-unit-hash-input]");
  if (!input) return;

  const attemptId = input.dataset.unitHashInput;
  try {
    input.value = window.sessionStorage.getItem(hashStorageKey(attemptId)) || "";
  } catch (_error) {
    // The operator can paste the wallet hash again if storage is unavailable.
  }

  const rejection = readWalletRejection(attemptId);
  if (!rejection?.rejectedUrl) return;

  const wrapper = input.closest(".flex");
  if (!wrapper || wrapper.querySelector("[data-unit-retry-rejection]")) return;

  const button = document.createElement("button");
  button.type = "button";
  button.className =
    "inline-flex h-[36px] items-center justify-center rounded-[7px] border border-[#CCD3C9] bg-white px-3 text-[12px] font-semibold text-[#17211A] hover:bg-[#F9FAF7]";
  button.dataset.unitRetryRejection = "";
  button.textContent = gettext("Registrar recusa");
  button.addEventListener("click", () =>
    recordWalletRejection(attemptId, rejection.rejectedUrl, button),
  );
  wrapper.appendChild(button);
}

async function registerHash(input, button) {
  const attemptId = input.dataset.unitHashInput;
  const transactionHash = input.value.trim();
  if (!transactionHash) {
    showMessage(gettext("Informe a hash retornada pela carteira."));
    return;
  }

  rememberHash(attemptId, transactionHash);
  setBusy(button, true);
  showMessage(gettext("Conferindo a hash na rede…"));
  try {
    const result = await postForm(input.dataset.unitHashUrl, {
      transaction_hash: transactionHash,
    });
    if (result.status === "waiting_for_rpc") {
      showMessage(result.message);
      setBusy(button, false);
      return;
    }
    clearHash(attemptId);
    showMessage(result.message);
    window.location.reload();
  } catch (error) {
    // Keep the same hash available; never prepare a second transaction here.
    showMessage(`${error.message} Tente novamente com esta mesma hash.`);
    setBusy(button, false);
  }
}

async function confirmRegistration(button) {
  setBusy(button, true);
  showMessage(gettext("Consultando a confirmação da rede…"));
  try {
    const result = await postForm(button.dataset.unitConfirmUrl);
    showMessage(result.message);
    if (result.status === "confirmed" || result.status === "failed") {
      window.location.reload();
      return;
    }
  } catch (error) {
    showMessage(error.message);
  }
  setBusy(button, false);
}

async function prepareAndSend(button) {
  if (!window.ethereum) {
    showMessage(gettext("Carteira compatível não encontrada neste navegador."));
    return;
  }

  setBusy(button, true);
  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    if (!accounts?.length) throw new Error(gettext("Conecte uma carteira para continuar."));

    const senderAddress = accounts[0];
    const chainHex = await window.ethereum.request({ method: "eth_chainId" });
    const walletChainId = Number.parseInt(chainHex, 16);
    const networkInput = document.getElementById("unit-registration-network");
    const networkId = button.dataset.networkId || networkInput?.value;
    const expectedChainId = Number(
      button.dataset.chainId || networkInput?.dataset.chainId,
    );

    if (!networkId || !Number.isSafeInteger(expectedChainId)) {
      throw new Error(gettext("Selecione uma rede com UnitRegistry ativo."));
    }
    if (walletChainId !== expectedChainId) {
      showMessage(
        interpolate(
          gettext("A carteira está na rede %(network)s. Troque para %(chainId)s e tente novamente."),
          { network: walletChainId || gettext("desconhecida"), chainId: expectedChainId },
          true,
        ),
      );
      return;
    }

    const prepared = await postForm(registrationPanel.dataset.prepareUrl, {
      network_id: String(networkId),
      sender_address: senderAddress,
      wallet_chain_id: String(walletChainId),
    });

    if (prepared.status !== "ready") {
      // The server found a previous attempt. Reload to show its recovery action.
      showMessage(prepared.message);
      window.location.reload();
      return;
    }

    if (prepared.owner_address.toLowerCase() !== senderAddress.toLowerCase()) {
      throw new Error(gettext("Conecte a carteira dona do UnitRegistry."));
    }

    // Store the uncertain-send marker before asking the wallet to sign.
    await postForm(prepared.started_url);
    showMessage(gettext("Confirme o registro da unidade na carteira."));

    let transactionHash;
    try {
      transactionHash = await window.ethereum.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: senderAddress,
            to: prepared.contract_address,
            data: prepared.transaction_data,
            value: "0x0",
          },
        ],
      });
    } catch (error) {
      if (error?.code === 4001) {
        rememberWalletRejection(prepared.attempt_id, prepared.rejected_url);
        await recordWalletRejection(
          prepared.attempt_id,
          prepared.rejected_url,
          button,
        );
        return;
      }

      // Any other wallet error may leave the network result uncertain.
      showMessage(
        gettext("Não foi possível saber se a carteira enviou a transação. Não envie outra. Se ela mostrar uma hash, informe-a nesta tentativa após recarregar a página."),
      );
      window.location.reload();
      return;
    }

    rememberHash(prepared.attempt_id, transactionHash);
    showMessage(gettext("Hash recebida pela carteira. Conferindo a transação na rede…"));
    let result;
    try {
      result = await postForm(prepared.register_url, {
        transaction_hash: transactionHash,
      });
    } catch (error) {
      // The hash is retained in this browser so a refresh can retry this same attempt.
      showMessage(
        `${error.message} A hash ficou guardada nesta tela; recarregue para conferi-la novamente.`,
      );
      setBusy(button, true);
      return;
    }
    if (result.status === "waiting_for_rpc") {
      window.location.reload();
      return;
    }
    clearHash(prepared.attempt_id);
    showMessage(result.message);
    window.location.reload();
  } catch (error) {
    showMessage(error.message);
    setBusy(button, false);
  }
}

if (registrationPanel) {
  restoreAttemptRecovery();

  registrationPanel.addEventListener("click", (event) => {
    const prepareButton = event.target.closest("[data-unit-prepare]");
    if (prepareButton) {
      prepareAndSend(prepareButton);
      return;
    }

    const hashButton = event.target.closest("[data-unit-register-hash]");
    if (hashButton) {
      const input = registrationPanel.querySelector("[data-unit-hash-input]");
      if (input) registerHash(input, hashButton);
      return;
    }

    const confirmButton = event.target.closest("[data-unit-confirm-url]");
    if (confirmButton) confirmRegistration(confirmButton);
  });
}
