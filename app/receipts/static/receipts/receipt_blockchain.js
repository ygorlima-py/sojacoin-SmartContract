"use strict";

/**
 * Drive the receipt workflow with the user's injected wallet provider.
 *
 * The browser receives only server-encoded calldata. Django persists an
 * immutable attempt before the wallet prompt and validates every returned hash
 * against the selected network, receiving wallet, contract, and saved payload.
 */
(() => {
  const root = document.getElementById("receipt-blockchain");
  if (!root) return;

  const message = document.getElementById("receipt-blockchain-message");
  const networkSelect = document.getElementById(root.dataset.networkSelect || "");
  const csrfToken = root.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  const provider = window.ethereum;

  function showMessage(text, tone = "info") {
    if (!message) return;
    const tones = {
      info: "border-[#DCE4DC] bg-[#F7F9F6] text-[#46564A]",
      success: "border-[#CFE2D3] bg-[#F4F8F4] text-[#1E6B3C]",
      warning: "border-[#F0D6A4] bg-[#FFF9EA] text-[#76530E]",
      error: "border-[#E8C5C0] bg-[#FFF6F4] text-[#A33C2E]",
    };
    message.className = `rounded-[7px] border px-3 py-2 text-[12px] ${tones[tone] || tones.info}`;
    message.textContent = text;
  }

  function attemptUrl(attemptId, action) {
    return `${root.dataset.attemptUrlPrefix}${attemptId}/${action}/`;
  }

  async function post(url, values = {}) {
    const body = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) body.set(key, String(value));
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRFToken": csrfToken,
        Accept: "application/json",
      },
      body,
    });
    let result;
    try {
      result = await response.json();
    } catch (_error) {
      throw new Error(
        response.status === 403
          ? "Seu usuário não tem autorização para operar este recebimento."
          : "O servidor não retornou uma resposta válida. Atualize a página e tente novamente.",
      );
    }
    if (!response.ok) throw new Error(result.error || "Não foi possível concluir esta ação.");
    return result;
  }

  function selectedNetwork() {
    if (networkSelect) {
      const option = networkSelect.selectedOptions[0];
      if (!option || !networkSelect.value) throw new Error("Selecione uma rede disponível.");
      return { id: networkSelect.value, chainId: Number(option.dataset.chainId) };
    }
    const id = root.dataset.pinnedNetworkId;
    const chainId = root.dataset.pinnedChainId;
    if (!id || !chainId) throw new Error("A operação não tem uma rede selecionada.");
    return { id, chainId: Number(chainId) };
  }

  async function connectReceivingWallet(expectedChainId) {
    if (!provider || typeof provider.request !== "function") {
      throw new Error("Abra esta página em um navegador com uma carteira EVM, como a MetaMask.");
    }
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    const sender = Array.isArray(accounts) ? accounts[0] : "";
    if (!sender) throw new Error("A carteira não retornou uma conta conectada.");
    const chainIdHex = await provider.request({ method: "eth_chainId" });
    const actualChainId = Number.parseInt(chainIdHex, 16);
    if (actualChainId !== expectedChainId) {
      throw new Error(`A MetaMask está na rede ${actualChainId}. Troque para a rede do processo (ID ${expectedChainId}).`);
    }
    return sender.toLowerCase();
  }

  function createRecoveryForm(attemptId, value = "") {
    let form = root.querySelector(`[data-receipt-hash-form="${attemptId}"]`);
    if (form) {
      const input = form.querySelector("[name=transaction_hash]");
      if (input && value) input.value = value;
      return form;
    }
    const currentCard = root.querySelector("button[data-receipt-send]")?.closest("li");
    if (!currentCard) return null;
    const wrapper = currentCard.querySelector(".w-full.shrink-0") || currentCard;
    form = document.createElement("form");
    form.dataset.receiptHashForm = String(attemptId);
    form.className = "mt-2 flex w-full flex-col gap-2 sm:w-[300px]";
    const label = document.createElement("label");
    label.className = "text-[10px] font-medium text-[#657268]";
    label.htmlFor = `receipt-hash-${attemptId}`;
    label.textContent = "Hash retornada pela carteira";
    const input = document.createElement("input");
    input.id = label.htmlFor;
    input.name = "transaction_hash";
    input.inputMode = "text";
    input.autocomplete = "off";
    input.placeholder = "0x…";
    input.required = true;
    input.value = value;
    input.className = "h-[40px] w-full rounded-[7px] border border-[#D7DDD4] px-3 font-mono text-[10px] outline-none focus:border-[#1E6B3C]";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "inline-flex min-h-[40px] items-center justify-center gap-2 rounded-[7px] border border-[#C7D9CB] bg-white px-4 text-[11px] font-semibold text-[#1E6B3C] hover:bg-[#F4F8F4]";
    submit.textContent = "Conferir hash";
    form.append(label, input, submit);
    wrapper.append(form);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      registerHash(form);
    });
    return form;
  }

  function cachedHash(attemptId) {
    try {
      return window.sessionStorage.getItem(`sojacoin:receipt-attempt:${attemptId}`) || "";
    } catch (_error) {
      return "";
    }
  }

  async function sendNextCall(button) {
    button.disabled = true;
    try {
      const network = selectedNetwork();
      const sender = await connectReceivingWallet(network.chainId);
      const prepared = await post(root.dataset.prepareUrl, {
        network_id: network.id,
        sender_address: sender,
      });

      if (prepared.status === "submitted") {
        showMessage("Esta etapa já foi enviada. Consulte a confirmação da transação existente.", "warning");
        window.location.reload();
        return;
      }
      if (prepared.status === "recover") {
        button.hidden = true;
        createRecoveryForm(prepared.attempt_id, cachedHash(prepared.attempt_id));
        showMessage("O envio desta etapa ficou incerto. Informe a hash da carteira para recuperar a mesma tentativa; ela não será enviada novamente.", "warning");
        return;
      }
      if (Number(prepared.chain_id) !== network.chainId) {
        throw new Error("A rede mudou entre a preparação e a abertura da carteira. Atualize a página.");
      }

      const attemptId = prepared.attempt_id;
      await post(attemptUrl(attemptId, "started"), { sender_address: sender });
      let transactionHash;
      try {
        transactionHash = await provider.request({
          method: "eth_sendTransaction",
          params: [
            {
              from: sender,
              to: prepared.contract_address,
              data: prepared.transaction_data,
              value: "0x0",
            },
          ],
        });
      } catch (error) {
        if (error && Number(error.code) === 4001) {
          await post(attemptUrl(attemptId, "rejected"));
          showMessage("A carteira recusou a solicitação. Você pode iniciar uma nova tentativa.", "warning");
          window.location.reload();
          return;
        }
        button.hidden = true;
        createRecoveryForm(attemptId, cachedHash(attemptId));
        showMessage("A carteira não informou se transmitiu a chamada. Não envie de novo: recupere esta tentativa usando a hash, se ela aparecer na carteira.", "warning");
        return;
      }

      try {
        window.sessionStorage.setItem(`sojacoin:receipt-attempt:${attemptId}`, transactionHash);
      } catch (_error) {
        // The hash remains visible to the operator and can be copied manually.
      }
      const registered = await post(attemptUrl(attemptId, "hash"), {
        transaction_hash: transactionHash,
      });
      if (registered.status === "pending") {
        button.hidden = true;
        const form = createRecoveryForm(attemptId, transactionHash);
        form?.scrollIntoView({ behavior: "smooth", block: "center" });
        showMessage(registered.message || "O RPC ainda não encontrou a hash. A tentativa foi preservada para nova conferência.", "warning");
        return;
      }
      try {
        window.sessionStorage.removeItem(`sojacoin:receipt-attempt:${attemptId}`);
      } catch (_error) {
        // Session storage is optional; the database record is authoritative.
      }
      showMessage("Hash conferida. A rede ainda precisa confirmar a transação.", "success");
      window.location.reload();
    } catch (error) {
      showMessage(error.message || "Não foi possível preparar esta chamada.", "error");
    } finally {
      button.disabled = false;
    }
  }

  async function registerHash(form) {
    const attemptId = form.dataset.receiptHashForm;
    const input = form.querySelector("[name=transaction_hash]");
    const button = form.querySelector("button[type=submit]");
    if (!input || !button) return;
    button.disabled = true;
    try {
      const result = await post(attemptUrl(attemptId, "hash"), {
        transaction_hash: input.value.trim(),
      });
      if (result.status === "pending") {
        showMessage(result.message || "O RPC ainda não encontrou esta hash. Tente novamente com a mesma hash.", "warning");
        return;
      }
      try {
        window.sessionStorage.removeItem(`sojacoin:receipt-attempt:${attemptId}`);
      } catch (_error) {
        // Session storage is optional; the database record is authoritative.
      }
      showMessage("Hash conferida. A rede ainda precisa confirmar a transação.", "success");
      window.location.reload();
    } catch (error) {
      showMessage(error.message || "Não foi possível conferir esta hash.", "error");
    } finally {
      button.disabled = false;
    }
  }

  async function checkConfirmation(button) {
    const attemptId = button.dataset.receiptConfirm;
    button.disabled = true;
    try {
      const result = await post(attemptUrl(attemptId, "confirm"));
      if (result.status === "pending") {
        showMessage("A rede ainda não confirmou esta transação. Consulte novamente depois.", "warning");
        return;
      }
      if (result.status === "failed") {
        showMessage("A transação foi revertida. A etapa pode ser preparada novamente.", "error");
        window.location.reload();
        return;
      }
      if (result.status === "needs_review") {
        showMessage(
          "A transação exige conferência: os dados confirmados diferem do esperado. Nenhuma etapa posterior será liberada.",
          "error",
        );
        window.location.reload();
        return;
      }
      showMessage(
        result.deposit_id
          ? `Etapa confirmada. ID do recebimento na rede: ${result.deposit_id}.`
          : "Etapa confirmada na blockchain.",
        "success",
      );
      window.location.reload();
    } catch (error) {
      showMessage(error.message || "Não foi possível consultar a confirmação.", "error");
    } finally {
      button.disabled = false;
    }
  }

  root.querySelectorAll("button[data-receipt-send]").forEach((button) => {
    button.addEventListener("click", () => sendNextCall(button));
  });
  root.querySelectorAll("button[data-receipt-confirm]").forEach((button) => {
    button.addEventListener("click", () => checkConfirmation(button));
  });
  root.querySelectorAll("form[data-receipt-hash-form]").forEach((form) => {
    const attemptId = form.dataset.receiptHashForm;
    const hashInput = form.querySelector("[name=transaction_hash]");
    if (hashInput && !hashInput.value) hashInput.value = cachedHash(attemptId);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      registerHash(form);
    });
  });
})();
