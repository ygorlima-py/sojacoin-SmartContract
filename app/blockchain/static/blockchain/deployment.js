"use strict";

const deploymentConfig = document.getElementById("deployment-wallet-config");
const expectedChainId = Number(deploymentConfig?.dataset.chainId);

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
    // An HTML error page can be returned if the server failed before JSON output.
  }
  if (!response.ok) {
    throw new Error(
      result.error ||
        result.message ||
        "O servidor não concluiu esta etapa. Confira a mensagem e tente novamente com a mesma hash.",
    );
  }
  return result;
}

function rowMessage(row, message) {
  const target = row.querySelector(".deployment-row-message");
  if (target) target.textContent = message;
}

function storageKey(attemptId) {
  return `sojacoin-deployment-hash-${attemptId}`;
}

function saveReturnedHash(attemptId, hash) {
  try {
    // Keep the wallet result available if recording it on the server needs a retry.
    window.sessionStorage.setItem(storageKey(attemptId), hash);
  } catch (_error) {
    // The current page still displays the hash even if storage is unavailable.
  }
}

function savedHash(attemptId) {
  try {
    return window.sessionStorage.getItem(storageKey(attemptId)) || "";
  } catch (_error) {
    return "";
  }
}

function clearSavedHash(attemptId) {
  try {
    window.sessionStorage.removeItem(storageKey(attemptId));
  } catch (_error) {
    // A stale value is harmless; the server remains the source of transaction state.
  }
}

function configurationStorageKey(attemptId) {
  return `sojacoin-configuration-hash-${attemptId}`;
}

function saveConfigurationHash(attemptId, hash) {
  try {
    // Preserve a MetaMask result across refresh if the RPC is temporarily unavailable.
    window.sessionStorage.setItem(configurationStorageKey(attemptId), hash);
  } catch (_error) {
    // The visible recovery input remains available for this page.
  }
}

function savedConfigurationHash(attemptId) {
  try {
    return window.sessionStorage.getItem(configurationStorageKey(attemptId)) || "";
  } catch (_error) {
    return "";
  }
}

function clearConfigurationHash(attemptId) {
  try {
    window.sessionStorage.removeItem(configurationStorageKey(attemptId));
  } catch (_error) {
    // The server remains authoritative even if browser storage is unavailable.
  }
}

function recoveryForm(row, attemptId, registerUrl) {
  let input = row.querySelector("[data-register-url]");
  if (input) return input;

  const wrapper = document.createElement("div");
  wrapper.className = "deployment-recovery";
  input = document.createElement("input");
  input.type = "text";
  input.maxLength = 66;
  input.placeholder = "Hash da carteira";
  input.setAttribute("aria-label", "Hash da transação");
  input.dataset.attemptId = String(attemptId);
  input.dataset.registerUrl = registerUrl;
  input.value = savedHash(attemptId);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "button";
  button.dataset.registerHash = "";
  button.textContent = "Conferir hash";

  const message = document.createElement("span");
  message.className = "deployment-row-message";
  message.setAttribute("role", "status");
  wrapper.append(input, button, message);
  const actionCell = row.querySelector("td:last-child") || row;
  actionCell.appendChild(wrapper);
  return input;
}

async function registerHash(row, input, hash = input.value.trim()) {
  const attemptId = input.dataset.attemptId;
  const registerUrl = input.dataset.registerUrl;
  if (!hash) {
    rowMessage(row, "Informe a hash exibida pela carteira.");
    return;
  }

  saveReturnedHash(attemptId, hash);
  input.value = hash;
  rowMessage(row, `Hash recebida: ${hash}. Conferindo na rede…`);
  try {
    const result = await postForm(registerUrl, { transaction_hash: hash });
    if (result.status === "waiting_for_rpc") {
      rowMessage(row, result.message);
      return;
    }
    clearSavedHash(attemptId);
    rowMessage(row, result.message);
    window.location.reload();
  } catch (error) {
    // Do not create another attempt: the saved send-intent remains open for this hash.
    rowMessage(row, `${error.message} Tente novamente com esta mesma hash.`);
  }
}

async function beginDeployment(button) {
  const row = button.closest("tr");
  if (!window.ethereum) {
    rowMessage(row, "MetaMask não encontrada neste navegador.");
    return;
  }

  button.disabled = true;
  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    if (!accounts || !accounts.length) {
      throw new Error("Conecte uma carteira para continuar.");
    }
    const operatorAddress = accounts[0];
    const walletChainHex = await window.ethereum.request({ method: "eth_chainId" });
    const walletChainId = Number.parseInt(walletChainHex, 16);
    if (!Number.isSafeInteger(walletChainId) || walletChainId !== expectedChainId) {
      rowMessage(
        row,
        `A MetaMask está na rede ${walletChainId || "desconhecida"}. ` +
          `Troque para a rede do processo (ID ${expectedChainId}).`,
      );
      return;
    }

    const prepared = await postForm(button.dataset.prepareUrl, {
      operator_address: operatorAddress,
      wallet_chain_id: String(walletChainId),
    });
    if (prepared.chain_id !== expectedChainId) {
      throw new Error("A rede informada pelo servidor não corresponde a esta carteira.");
    }

    // Mark the attempt uncertain before opening the wallet's transaction prompt.
    try {
      await postForm(prepared.started_url);
    } catch (error) {
      recoveryForm(row, prepared.attempt_id, prepared.register_url);
      rowMessage(
        row,
        `Não foi possível confirmar o estado do pedido no servidor (${error.message}). ` +
          "A MetaMask não será aberta; recarregue a página antes de continuar.",
      );
      return;
    }
    rowMessage(row, "Confirme a criação do contrato na MetaMask.");

    let transactionHash;
    try {
      transactionHash = await window.ethereum.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: operatorAddress,
            data: prepared.transaction_data,
          },
        ],
      });
    } catch (error) {
      if (error && error.code === 4001) {
        try {
          await postForm(prepared.rejected_url);
          rowMessage(row, "Envio cancelado na carteira. Você pode tentar novamente.");
          window.location.reload();
        } catch (recordError) {
          rowMessage(
            row,
            `A carteira recusou, mas não foi possível registrar a resposta: ${recordError.message}`,
          );
        }
        return;
      }

      recoveryForm(row, prepared.attempt_id, prepared.register_url);
      rowMessage(
        row,
        "Não foi possível saber se a transação foi enviada. Não tente enviar outra. " +
          "Se a carteira mostrar uma hash, informe-a abaixo para conferir esta tentativa.",
      );
      return;
    }

    saveReturnedHash(prepared.attempt_id, transactionHash);
    const input = recoveryForm(row, prepared.attempt_id, prepared.register_url);
    input.value = transactionHash;
    await registerHash(row, input, transactionHash);
  } catch (error) {
    rowMessage(row, error.message);
  } finally {
    button.disabled = false;
  }
}

async function confirmDeployment(row, button) {
  button.disabled = true;
  rowMessage(row, "Consultando recibo e endereço do contrato no RPC…");
  try {
    const result = await postForm(button.dataset.confirmUrl);
    rowMessage(row, result.message);
    if (result.status !== "pending") window.location.reload();
  } catch (error) {
    rowMessage(row, `${error.message} Consulte novamente esta mesma transação.`);
  } finally {
    button.disabled = false;
  }
}

function configurationRecoveryForm(row, attemptId, registerUrl) {
  let input = row.querySelector("[data-register-configuration-url]");
  if (input) return input;

  const wrapper = document.createElement("div");
  wrapper.className = "deployment-recovery";
  input = document.createElement("input");
  input.type = "text";
  input.maxLength = 66;
  input.placeholder = "Hash da carteira";
  input.setAttribute("aria-label", "Hash da chamada de configuração");
  input.dataset.configurationAttemptId = String(attemptId);
  input.dataset.registerConfigurationUrl = registerUrl;
  input.value = savedConfigurationHash(attemptId);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "button";
  button.dataset.registerConfigurationHash = "";
  button.textContent = "Conferir hash";

  const message = document.createElement("span");
  message.className = "deployment-row-message";
  message.setAttribute("role", "status");
  wrapper.append(input, button, message);
  (row.querySelector("td:last-child") || row).appendChild(wrapper);
  return input;
}

async function registerConfigurationHash(row, input, hash = input.value.trim()) {
  const attemptId = input.dataset.configurationAttemptId;
  if (!hash) {
    rowMessage(row, "Informe a hash retornada pela carteira.");
    return;
  }

  saveConfigurationHash(attemptId, hash);
  input.value = hash;
  rowMessage(row, `Hash recebida: ${hash}. Conferindo na rede…`);
  try {
    const result = await postForm(input.dataset.registerConfigurationUrl, {
      transaction_hash: hash,
    });
    if (result.status === "waiting_for_rpc") {
      rowMessage(row, result.message);
      return;
    }
    clearConfigurationHash(attemptId);
    rowMessage(row, result.message);
    window.location.reload();
  } catch (error) {
    rowMessage(row, `${error.message} Tente novamente com esta mesma hash.`);
  }
}

async function beginConfiguration(button) {
  const row = button.closest("tr");
  if (!window.ethereum) {
    rowMessage(row, "MetaMask não encontrada neste navegador.");
    return;
  }

  button.disabled = true;
  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    if (!accounts || !accounts.length) {
      throw new Error("Conecte a carteira dona do contrato para continuar.");
    }
    const ownerAddress = accounts[0];
    const walletChainHex = await window.ethereum.request({ method: "eth_chainId" });
    const walletChainId = Number.parseInt(walletChainHex, 16);
    if (!Number.isSafeInteger(walletChainId) || walletChainId !== expectedChainId) {
      rowMessage(
        row,
        `A MetaMask está na rede ${walletChainId || "desconhecida"}. ` +
          `Troque para a rede do processo (ID ${expectedChainId}).`,
      );
      return;
    }

    const prepared = await postForm(button.dataset.configureUrl, {
      operator_address: ownerAddress,
      wallet_chain_id: String(walletChainId),
    });
    if (prepared.status === "already_configured") {
      rowMessage(row, prepared.message);
      return;
    }
    if (prepared.status === "needs_review") {
      rowMessage(row, prepared.message);
      window.location.reload();
      return;
    }
    if (prepared.chain_id !== expectedChainId) {
      throw new Error("A rede informada pelo servidor não corresponde a esta carteira.");
    }
    if (prepared.owner_address.toLowerCase() !== ownerAddress.toLowerCase()) {
      throw new Error("Conecte a carteira dona atual do contrato SojaCoin.");
    }

    // Record that a wallet prompt is about to open before asking MetaMask to send.
    try {
      await postForm(prepared.started_url);
    } catch (error) {
      configurationRecoveryForm(row, prepared.attempt_id, prepared.register_url);
      rowMessage(
        row,
        `Não foi possível confirmar o estado do pedido (${error.message}). ` +
          "A MetaMask não será aberta; recarregue antes de continuar.",
      );
      return;
    }

    rowMessage(row, "Confirme a configuração na MetaMask.");
    let transactionHash;
    try {
      transactionHash = await window.ethereum.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: ownerAddress,
            to: prepared.transaction_to,
            data: prepared.transaction_data,
            value: "0x0",
          },
        ],
      });
    } catch (error) {
      if (error && error.code === 4001) {
        try {
          await postForm(prepared.rejected_url);
          rowMessage(row, "A carteira recusou a configuração. Você pode tentar novamente.");
          window.location.reload();
        } catch (recordError) {
          rowMessage(
            row,
            `A carteira recusou, mas o servidor não registrou a resposta: ${recordError.message}`,
          );
        }
        return;
      }

      configurationRecoveryForm(row, prepared.attempt_id, prepared.register_url);
      rowMessage(
        row,
        "Não foi possível saber se a chamada foi enviada. Não envie outra; informe a hash nesta mesma tentativa.",
      );
      return;
    }

    saveConfigurationHash(prepared.attempt_id, transactionHash);
    const input = configurationRecoveryForm(
      row,
      prepared.attempt_id,
      prepared.register_url,
    );
    input.value = transactionHash;
    await registerConfigurationHash(row, input, transactionHash);
  } catch (error) {
    rowMessage(row, error.message);
  } finally {
    button.disabled = false;
  }
}

async function confirmConfiguration(row, button) {
  button.disabled = true;
  rowMessage(row, "Consultando recibo e vínculo no RPC…");
  try {
    const result = await postForm(button.dataset.confirmConfigurationUrl);
    rowMessage(row, result.message);
    if (result.status !== "pending") window.location.reload();
  } catch (error) {
    rowMessage(row, `${error.message} Consulte novamente esta mesma chamada.`);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const prepareButton = event.target.closest("[data-prepare-url]");
  if (prepareButton) {
    beginDeployment(prepareButton);
    return;
  }

  const registerButton = event.target.closest("[data-register-hash]");
  if (registerButton) {
    const row = registerButton.closest("tr");
    const input = row.querySelector("[data-register-url]");
    registerHash(row, input);
    return;
  }

  const confirmButton = event.target.closest("[data-confirm-url]");
  if (confirmButton) {
    confirmDeployment(confirmButton.closest("tr"), confirmButton);
    return;
  }

  const configureButton = event.target.closest("[data-configure-url]");
  if (configureButton) {
    beginConfiguration(configureButton);
    return;
  }

  const registerConfigurationButton = event.target.closest(
    "[data-register-configuration-hash]",
  );
  if (registerConfigurationButton) {
    const row = registerConfigurationButton.closest("tr");
    const input = row.querySelector("[data-register-configuration-url]");
    registerConfigurationHash(row, input);
    return;
  }

  const confirmConfigurationButton = event.target.closest(
    "[data-confirm-configuration-url]",
  );
  if (confirmConfigurationButton) {
    confirmConfiguration(
      confirmConfigurationButton.closest("tr"),
      confirmConfigurationButton,
    );
  }
});

// Restore a wallet hash after a refresh so the operator can retry registration.
document.querySelectorAll("[data-register-url][data-attempt-id]").forEach((input) => {
  input.value = savedHash(input.dataset.attemptId);
});

document
  .querySelectorAll("[data-register-configuration-url][data-configuration-attempt-id]")
  .forEach((input) => {
    input.value = savedConfigurationHash(input.dataset.configurationAttemptId);
  });
