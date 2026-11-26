"use strict";

/**
 * Run staff wallet actions from a confirmed receipt page.
 *
 * Django persists the call before MetaMask opens. The browser only broadcasts
 * the saved calldata; RPC hash validation and event reconciliation stay server-side.
 */
(() => {
  const root = document.getElementById("issuance-batch-ui");
  if (!root) return;

  const message = document.getElementById("issuance-batch-message");
  const csrfToken = root.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  const provider = window.ethereum;

  /** Display an accessible status message using the project's existing palette. */
  function showMessage(text, tone = "info") {
    if (!message) return;
    const styles = {
      info: "border-[#DCE4DC] bg-[#F7F9F6] text-[#46564A]",
      success: "border-[#CFE2D3] bg-[#F4F8F4] text-[#1E6B3C]",
      warning: "border-[#F0D6A4] bg-[#FFF9EA] text-[#76530E]",
      error: "border-[#E8C5C0] bg-[#FFF6F4] text-[#A33C2E]",
    };
    message.className = `rounded-[7px] border px-3 py-2 text-[12px] ${styles[tone] || styles.info}`;
    message.textContent = gettext(text);
  }

  /** Send a CSRF-protected form request and normalize server error messages. */
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
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || gettext("Não foi possível concluir esta ação."));
    return result;
  }

  /** Connect the operator wallet and ensure MetaMask is on the pinned receipt network. */
  async function connectWallet(expectedChainId) {
    if (!provider || typeof provider.request !== "function") {
      throw new Error(gettext("Abra esta página em um navegador com uma carteira EVM, como a MetaMask."));
    }
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    const sender = Array.isArray(accounts) ? accounts[0] : "";
    if (!sender) throw new Error(gettext("A carteira não retornou uma conta conectada."));
    const chainHex = await provider.request({ method: "eth_chainId" });
    const chainId = Number.parseInt(chainHex, 16);
    if (chainId !== expectedChainId) {
      throw new Error(interpolate(
        gettext("A MetaMask está na rede %(chainId)s. Troque para a rede do recebimento (ID %(expected)s)."),
        { chainId, expected: expectedChainId },
        true,
      ));
    }
    return sender.toLowerCase();
  }

  /** Build the scoped transaction-attempt endpoint for this batch and action. */
  function attemptUrl(batchId, attemptId, action) {
    const template = root.dataset.attemptUrlTemplate;
    const url = new URL(template, window.location.origin);
    url.pathname = url.pathname.replace(/\/0\/attempt\/0\/action\/$/, `/${batchId}/attempt/${attemptId}/${action}/`);
    return url.toString();
  }

  /** Ask MetaMask to broadcast one already persisted call and record its hash. */
  async function submitPrepared(prepared, sender) {
    if (prepared.status === "submitted") {
      showMessage(gettext("Esta ação já foi enviada. Atualizando a situação da tentativa existente."), "warning");
      window.location.reload();
      return;
    }
    if (prepared.status === "recover") {
      showMessage(gettext("O envio anterior ficou incerto. Use a mesma hash retornada pela carteira; não envie outra transação."), "warning");
      window.location.reload();
      return;
    }
    if (Number(prepared.chain_id) !== Number(root.dataset.chainId)) {
      throw new Error(gettext("A rede mudou entre a preparação e a abertura da carteira. Atualize a página."));
    }
    const endpoint = (action) => attemptUrl(prepared.batch_id, prepared.attempt_id, action);
    await post(endpoint("started"), { sender_address: sender });
    let transactionHash;
    try {
      transactionHash = await provider.request({
        method: "eth_sendTransaction",
        params: [{ from: sender, to: prepared.contract_address, data: prepared.transaction_data, value: "0x0" }],
      });
    } catch (error) {
      if (error && Number(error.code) === 4001) {
        await post(endpoint("rejected"));
        showMessage(gettext("A carteira recusou a solicitação. Você pode tentar novamente."), "warning");
        window.location.reload();
        return;
      }
      showMessage(gettext("A carteira não informou se transmitiu. Recupere esta tentativa pela hash, sem reenviar."), "warning");
      window.location.reload();
      return;
    }
    const result = await post(endpoint("hash"), { transaction_hash: transactionHash });
    if (result.status === "pending") {
      showMessage(result.message || gettext("O RPC ainda não encontrou esta hash. A tentativa foi preservada."), "warning");
      window.location.reload();
      return;
    }
    showMessage(gettext("Hash conferida. Aguardando confirmação da rede."), "success");
    window.location.reload();
  }

  /** Create or retry the batch using its saved deadline and verified operator wallet. */
  async function createBatch(button) {
    button.disabled = true;
    try {
      const chainId = Number(root.dataset.chainId);
      if (!chainId) throw new Error(gettext("Este recebimento ainda não tem uma rede confirmada."));
      const sender = await connectWallet(chainId);
      const deadlineInput = document.getElementById("issuance-batch-deadline");
      if (!deadlineInput?.value) throw new Error(gettext("Informe o prazo de aprovação do participante."));
      const deadline = Math.floor(new Date(deadlineInput.value).getTime() / 1000);
      const prepared = await post(root.dataset.prepareUrl, { sender_address: sender, deadline });
      await submitPrepared(prepared, sender);
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível preparar o lote."), "error");
    } finally {
      button.disabled = false;
    }
  }

  /** Generate a replacement participant link and expose it for copying once. */
  async function createApprovalLink(button) {
    button.disabled = true;
    try {
      const result = await post(root.dataset.linkUrl);
      const wrapper = root.querySelector("[data-batch-link-result]");
      const input = document.getElementById("batch-approval-url");
      if (!wrapper || !input) throw new Error(gettext("O campo do link não está disponível nesta página."));
      input.value = result.url;
      wrapper.classList.remove("hidden");
      wrapper.scrollIntoView({ behavior: "smooth", block: "center" });
      showMessage(gettext("Link criado. Copie e encaminhe ao participante; gerar outro invalida este."), "success");
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível criar o link."), "error");
    } finally {
      button.disabled = false;
    }
  }

  /** Prepare a cancellation request only after the selected wallet is connected. */
  async function cancelBatch(button) {
    button.disabled = true;
    try {
      const sender = await connectWallet(Number(root.dataset.chainId));
      const prepared = await post(root.dataset.cancelPrepareUrl, { sender_address: sender });
      await submitPrepared(prepared, sender);
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível preparar o cancelamento."), "error");
    } finally {
      button.disabled = false;
    }
  }

  /** Attach a wallet-returned hash to the same saved transaction attempt. */
  async function recoverHash(form) {
    const attemptId = form.dataset.batchHashForm;
    const hash = form.querySelector("[name=transaction_hash]")?.value.trim();
    const batchId = root.dataset.batchId;
    if (!hash || !batchId) return;
    try {
      const result = await post(attemptUrl(batchId, attemptId, "hash"), { transaction_hash: hash });
      if (result.status === "pending") {
        showMessage(result.message, "warning");
        return;
      }
      showMessage(gettext("Hash conferida. Aguardando confirmação da rede."), "success");
      window.location.reload();
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível conferir a hash."), "error");
    }
  }

  /** Poll one submitted transaction and refresh when the chain reaches a final state. */
  async function confirmBatch(button) {
    button.disabled = true;
    try {
      const result = await post(attemptUrl(root.dataset.batchId, button.dataset.batchConfirm, "confirm"));
      if (result.status === "pending") {
        showMessage(gettext("A rede ainda não confirmou esta transação. A consulta continuará enquanto a página estiver aberta."), "warning");
        button.disabled = false;
        return;
      }
      showMessage(result.status === "confirmed" ? gettext("Transação confirmada na blockchain.") : gettext("A transação terminou; confira o estado atualizado do lote."), result.status === "confirmed" ? "success" : "warning");
      window.location.reload();
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível consultar a confirmação."), "error");
      button.disabled = false;
    }
  }

  root.querySelectorAll("[data-batch-create]").forEach((button) => button.addEventListener("click", () => createBatch(button)));
  root.querySelectorAll("[data-batch-create-link]").forEach((button) => button.addEventListener("click", () => createApprovalLink(button)));
  root.querySelectorAll("[data-batch-cancel]").forEach((button) => button.addEventListener("click", () => cancelBatch(button)));
  root.querySelectorAll("[data-batch-confirm]").forEach((button) => button.addEventListener("click", () => confirmBatch(button)));
  root.querySelectorAll("[data-batch-hash-form]").forEach((form) => form.addEventListener("submit", (event) => {
    event.preventDefault();
    recoverHash(form);
  }));

  root.querySelector("[data-batch-copy-link]")?.addEventListener("click", async () => {
    const input = document.getElementById("batch-approval-url");
    if (!input?.value) return;
    try {
      await navigator.clipboard.writeText(input.value);
      showMessage(gettext("Link copiado."), "success");
    } catch (_error) {
      input.select();
      document.execCommand("copy");
      showMessage(gettext("Link selecionado para copiar."), "success");
    }
  });

  /** Recheck pending staff-owned calls shortly after the page opens. */
  const pendingButtons = [...root.querySelectorAll("[data-batch-confirm]")];
  if (pendingButtons.length) {
    let remainingPolls = 20;
    const timer = window.setInterval(() => {
      if (remainingPolls-- <= 0) {
        window.clearInterval(timer);
        return;
      }
      pendingButtons.forEach((button) => {
        if (!button.disabled) confirmBatch(button);
      });
    }, 6000);
  }
})();
