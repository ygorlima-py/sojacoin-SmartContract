"use strict";

/** Handle the participant's single-wallet-transaction approval flow. */
(() => {
  const root = document.getElementById("participant-batch-approval");
  if (!root) return;

  const message = document.getElementById("participant-batch-message");
  const provider = window.ethereum;

  /** Read Django's current CSRF cookie, falling back to the rendered token. */
  function getCsrfToken() {
    const cookiePrefix = "csrftoken=";
    const csrfCookie = document.cookie
      .split(";")
      .map((cookie) => cookie.trim())
      .find((cookie) => cookie.startsWith(cookiePrefix));
    if (csrfCookie) return decodeURIComponent(csrfCookie.slice(cookiePrefix.length));
    return root.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  }

  /** Show a concise operation result without inserting server text as HTML. */
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

  /** Send a same-origin POST with Django's current CSRF token. */
  async function post(url, values = {}) {
    const body = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) body.set(key, String(value));
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRFToken": getCsrfToken(),
        Accept: "application/json",
      },
      body,
    });
    const result = await response.json().catch(() => ({}));
    if (response.status === 403 && !result.error) {
      throw new Error(gettext("A página ficou desatualizada. Reabra ou atualize o link e tente novamente."));
    }
    if (!response.ok) throw new Error(result.error || gettext("Não foi possível concluir esta ação."));
    return result;
  }

  /** Connect the participant's wallet and verify the batch network. */
  async function connectParticipantWallet() {
    if (!provider || typeof provider.request !== "function") {
      throw new Error(gettext("Abra o link em um navegador com a MetaMask ou outra carteira EVM."));
    }
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    const wallet = Array.isArray(accounts) ? accounts[0] : "";
    if (!wallet) throw new Error(gettext("A carteira não retornou uma conta conectada."));

    const chainHex = await provider.request({ method: "eth_chainId" });
    const chainId = Number.parseInt(chainHex, 16);
    if (chainId !== Number(root.dataset.chainId)) {
      throw new Error(interpolate(
        gettext("Troque a carteira para a rede do lote (ID %(chainId)s)."),
        { chainId: root.dataset.chainId },
        true,
      ));
    }
    return wallet.toLowerCase();
  }

  /** Build the private attempt endpoint for this link and saved wallet request. */
  function attemptUrl(attemptId, action) {
    return `${root.dataset.attemptUrlPrefix}${attemptId}/${action}/`;
  }

  /** Send an already-persisted request, or direct the participant to recovery. */
  async function submitPrepared(prepared, wallet) {
    if (prepared.status === "submitted") {
      showMessage(gettext("A transação já foi enviada. Consulte a confirmação abaixo."), "warning");
      window.location.reload();
      return;
    }
    if (prepared.status === "recover") {
      showMessage(gettext("O envio ficou incerto. Recupere a mesma hash; não envie outra transação."), "warning");
      window.location.reload();
      return;
    }

    await post(attemptUrl(prepared.attempt_id, "started"), { sender_address: wallet });
    let transactionHash;
    try {
      transactionHash = await provider.request({
        method: "eth_sendTransaction",
        params: [{
          from: wallet,
          to: prepared.contract_address,
          data: prepared.transaction_data,
          value: "0x0",
        }],
      });
    } catch (error) {
      if (error && Number(error.code) === 4001) {
        await post(attemptUrl(prepared.attempt_id, "rejected"));
        showMessage(gettext("Transação recusada. Você pode tentar novamente enquanto o lote estiver válido."), "warning");
      } else {
        showMessage(gettext("A carteira não informou o resultado. Consulte a atividade e recupere a mesma hash."), "warning");
      }
      window.location.reload();
      return;
    }

    const result = await post(attemptUrl(prepared.attempt_id, "hash"), {
      transaction_hash: transactionHash,
    });
    if (result.status === "pending") {
      showMessage(result.message || gettext("O RPC ainda não encontrou a hash. A tentativa foi preservada."), "warning");
    } else {
      showMessage(gettext("Transação enviada. Aguardando confirmação da rede."), "success");
    }
    window.location.reload();
  }

  /** Prepare and send the participant's single approve-and-issue transaction. */
  async function approveAndIssue(button) {
    button.disabled = true;
    try {
      const wallet = await connectParticipantWallet();
      const prepared = await post(root.dataset.prepareUrl, {
        wallet_address: wallet,
        chain_id: Number(root.dataset.chainId),
      });
      await submitPrepared(prepared, wallet);
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível preparar a transação."), "error");
    } finally {
      button.disabled = false;
    }
  }

  /** Prepare the separate participant-initiated cancellation action. */
  async function cancelBatch(button) {
    button.disabled = true;
    try {
      const wallet = await connectParticipantWallet();
      const prepared = await post(root.dataset.cancelUrl, { sender_address: wallet });
      await submitPrepared(prepared, wallet);
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível preparar o cancelamento."), "error");
    } finally {
      button.disabled = false;
    }
  }

  /** Poll a submitted transaction and refresh only when its state changes. */
  async function confirmAttempt(button) {
    button.disabled = true;
    try {
      const result = await post(attemptUrl(button.dataset.participantConfirm, "confirm"));
      if (result.status === "pending") {
        showMessage(gettext("A transação ainda aguarda confirmação da rede."), "warning");
        button.disabled = false;
        return;
      }
      showMessage(
        result.status === "confirmed" ? gettext("Aprovação e emissão confirmadas.") : "A transação terminou; confira a situação do lote.",
        result.status === "confirmed" ? "success" : "warning",
      );
      window.location.reload();
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível consultar a confirmação."), "error");
      button.disabled = false;
    }
  }

  /** Attach a wallet-returned hash to its original prepared attempt. */
  async function recoverHash(form) {
    const attemptId = form.dataset.participantHashForm;
    const hash = form.querySelector("[name=transaction_hash]")?.value.trim();
    if (!hash) return;
    try {
      const result = await post(attemptUrl(attemptId, "hash"), { transaction_hash: hash });
      if (result.status === "pending") {
        showMessage(result.message || gettext("O RPC ainda não encontrou a hash. Consulte novamente."), "warning");
        return;
      }
      showMessage(gettext("Hash conferida. Aguardando confirmação da rede."), "success");
      window.location.reload();
    } catch (error) {
      showMessage(error.message || gettext("Não foi possível conferir a hash."), "error");
    }
  }

  root.querySelector("[data-participant-approve]")?.addEventListener("click", (event) => {
    approveAndIssue(event.currentTarget);
  });
  root.querySelector("[data-participant-cancel]")?.addEventListener("click", (event) => {
    cancelBatch(event.currentTarget);
  });
  root.querySelectorAll("[data-participant-confirm]").forEach((button) => {
    button.addEventListener("click", () => confirmAttempt(button));
  });
  root.querySelectorAll("[data-participant-hash-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      recoverHash(form);
    });
  });

  /** Recheck pending participant transactions while this page remains open. */
  const confirmButtons = [...root.querySelectorAll("[data-participant-confirm]")];
  if (confirmButtons.length) {
    let remainingPolls = 20;
    const timer = window.setInterval(() => {
      if (remainingPolls-- <= 0) {
        window.clearInterval(timer);
        return;
      }
      confirmButtons.forEach((button) => {
        if (!button.disabled) confirmAttempt(button);
      });
    }, 6000);
  }
})();
