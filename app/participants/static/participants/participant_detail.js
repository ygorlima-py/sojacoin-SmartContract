"use strict";

/** Keep the operator-facing proposal flow small and preserve uncertain hashes. */
(() => {
    const proposalForm = document.querySelector("#participant-proposal-form");
    const messageBox = document.querySelector("#participant-action-message");
    const linkForm = document.querySelector("#participant-link-form");
    const linkResult = document.querySelector("#participant-link-result");

    function csrfToken() {
        return document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
    }

    function showMessage(message, kind = "info") {
        if (!messageBox) return;
        const colors = {
            info: "border-[#DDE3DA] bg-[#F9FAF7] text-[#344238]",
            success: "border-[#C9DFD0] bg-[#EEF7F0] text-[#1E6B3C]",
            error: "border-[#E9C5C1] bg-[#FFF0EE] text-[#A52B20]",
        };
        messageBox.className = `rounded-[8px] border px-3 py-2 text-[12px] ${colors[kind] || colors.info}`;
        messageBox.textContent = message;
        messageBox.classList.remove("hidden");
    }

    async function postJson(url, data = {}) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                "X-Requested-With": "XMLHttpRequest",
            },
            body: JSON.stringify(data),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || "A solicitação não pôde ser concluída.");
        return result;
    }

    function attemptUrl(template, attemptId) {
        return template.replace("/0/", `/${attemptId}/`);
    }

    function addHashRecovery(container, url, hash) {
        const form = document.createElement("form");
        form.className = "mt-3 flex flex-wrap gap-2";
        form.innerHTML = `<input name="transaction_hash" class="min-w-[230px] flex-1 rounded-[7px] border border-[#D7DDD4] px-3 py-2 font-mono text-[10px]" aria-label="Hash da transação" required><button class="min-h-[36px] rounded-[7px] bg-[#1E6B3C] px-3 text-[11px] font-semibold text-white">Tentar registrar esta hash</button>`;
        form.elements.transaction_hash.value = hash;
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                await postJson(url, { transaction_hash: form.elements.transaction_hash.value.trim() });
                window.location.reload();
            } catch (error) {
                showMessage(error.message, "error");
            }
        });
        container.appendChild(form);
    }

    async function connectWallet(expectedChainId, expectedAddress = "") {
        if (!window.ethereum) throw new Error("Não encontrei uma carteira Web3 neste navegador.");
        const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
        const address = String(accounts?.[0] || "").toLowerCase();
        if (!address) throw new Error("Nenhuma carteira foi conectada.");
        if (expectedAddress && address !== expectedAddress.toLowerCase()) {
            throw new Error("Conecte a carteira dona da SojaCoin usada nesta tentativa.");
        }
        const chainId = Number.parseInt(await window.ethereum.request({ method: "eth_chainId" }), 16);
        if (chainId !== Number(expectedChainId)) {
            throw new Error(`A carteira está na rede ${chainId}. Selecione a rede ${expectedChainId}.`);
        }
        return { address, chainId };
    }

    async function sendPreparedTransaction(prepared, endpoints, container) {
        const started = await postJson(endpoints.started, {});
        if (!started.send_allowed) {
            showMessage("Esta solicitação da carteira já foi aberta. Se o navegador perdeu a resposta, informe a hash na mesma tentativa.", "info");
            window.location.reload();
            return;
        }
        let transactionHash = "";
        try {
            transactionHash = await window.ethereum.request({
                method: "eth_sendTransaction",
                params: [{
                    from: prepared.from,
                    to: prepared.to,
                    data: prepared.data,
                    value: "0x0",
                }],
            });
        } catch (error) {
            if (error?.code === 4001) {
                await postJson(endpoints.rejected, {});
                showMessage("A solicitação foi recusada na carteira. Você pode tentar novamente.", "info");
            } else {
                showMessage("Não recebi uma resposta definitiva da carteira. Não envie outra transação; se ela mostrou uma hash, informe a mesma hash abaixo.", "error");
                window.location.reload();
            }
            return;
        }

        try {
            await postJson(endpoints.hash, { transaction_hash: transactionHash });
            showMessage("Hash registrada. A rede ainda precisa confirmar a transação.", "success");
            window.location.reload();
        } catch (error) {
            showMessage(`${error.message} A hash retornada foi preservada aqui para você tentar registrá-la na mesma tentativa.`, "error");
            addHashRecovery(container, endpoints.hash, transactionHash);
        }
    }

    proposalForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = proposalForm.querySelector("button[type=submit]");
        const networkSelect = proposalForm.querySelector("select[name=network_id]");
        const selected = networkSelect?.selectedOptions?.[0];
        if (!selected) return showMessage("Escolha uma rede que tenha SojaCoin ativa.", "error");
        if (button) button.disabled = true;
        try {
            const wallet = await connectWallet(Number(selected.dataset.chainId));
            const prepared = await postJson(proposalForm.dataset.prepareUrl, {
                network_id: networkSelect.value,
                owner_address: wallet.address,
                wallet_chain_id: wallet.chainId,
            });
            if (!prepared.send_allowed) {
                showMessage("Já existe uma tentativa aberta. Atualize a página e use a ação dessa tentativa para continuar ou informar a hash.", "info");
                window.location.reload();
                return;
            }
            const endpoints = {
                started: attemptUrl(proposalForm.dataset.startTemplate, prepared.attempt_id),
                hash: attemptUrl(proposalForm.dataset.hashTemplate, prepared.attempt_id),
                rejected: attemptUrl(proposalForm.dataset.rejectedTemplate, prepared.attempt_id),
            };
            await sendPreparedTransaction(prepared, endpoints, proposalForm);
        } catch (error) {
            showMessage(error.message, "error");
        } finally {
            if (button) button.disabled = false;
        }
    });

    document.querySelectorAll(".participant-resume-proposal").forEach((button) => {
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                const wallet = await connectWallet(Number(button.dataset.chainId), button.dataset.sender);
                const prepared = {
                    from: wallet.address,
                    to: button.dataset.to,
                    data: button.dataset.data,
                };
                await sendPreparedTransaction(
                    prepared,
                    {
                        started: button.dataset.startUrl,
                        hash: button.dataset.hashUrl,
                        rejected: button.dataset.rejectedUrl,
                    },
                    button.parentElement,
                );
            } catch (error) {
                showMessage(error.message, "error");
            } finally {
                button.disabled = false;
            }
        });
    });

    document.querySelectorAll(".participant-hash-form").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                await postJson(form.dataset.url, { transaction_hash: form.elements.transaction_hash.value.trim() });
                window.location.reload();
            } catch (error) {
                showMessage(error.message, "error");
            }
        });
    });

    document.querySelectorAll(".participant-confirm-form").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = form.querySelector("button");
            if (button) button.disabled = true;
            try {
                const result = await postJson(form.dataset.url, {});
                showMessage(result.message, result.status === "proposal_confirmed" ? "success" : "info");
                window.setTimeout(() => window.location.reload(), 1200);
            } catch (error) {
                showMessage(error.message, "error");
                if (button) button.disabled = false;
            }
        });
    });

    linkForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = linkForm.querySelector("button");
        if (button) button.disabled = true;
        try {
            const result = await postJson(linkForm.dataset.createUrl, {});
            linkResult.innerHTML = "";
            const label = document.createElement("p");
            label.className = "font-semibold text-[#1E6B3C]";
            label.textContent = `${result.message} Expira em ${new Date(result.expires_at).toLocaleString("pt-BR")}.`;
            const input = document.createElement("input");
            input.className = "mt-2 w-full rounded-[7px] border border-[#C9DFD0] bg-white px-3 py-2 font-mono text-[10px]";
            input.readOnly = true;
            input.value = result.url;
            input.setAttribute("aria-label", "Link de aceite para copiar");
            const copy = document.createElement("button");
            copy.type = "button";
            copy.className = "mt-2 min-h-[36px] rounded-[7px] bg-[#1E6B3C] px-3 text-[11px] font-semibold text-white";
            copy.textContent = "Copiar link";
            copy.addEventListener("click", async () => {
                await navigator.clipboard.writeText(result.url);
                copy.textContent = "Link copiado";
            });
            linkResult.append(label, input, copy);
            linkResult.classList.remove("hidden");
        } catch (error) {
            showMessage(error.message, "error");
        } finally {
            if (button) button.disabled = false;
        }
    });
})();
