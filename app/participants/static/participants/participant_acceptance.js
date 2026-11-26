"use strict";

/** Sign the displayed terms with EIP-712, then send its separate chain call. */
(() => {
    const root = document.querySelector("#acceptance-flow");
    if (!root) return;

    const message = document.querySelector("#acceptance-message");
    const recovery = document.querySelector("#acceptance-recovery");
    const expectedWallet = root.dataset.wallet.toLowerCase();
    const expectedChain = Number(root.dataset.chainId);

    function csrfToken() {
        return document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
    }

    function showMessage(text, kind = "info") {
        const colors = {
            info: "border-[#DDE3DA] bg-[#F9FAF7] text-[#344238]",
            success: "border-[#C9DFD0] bg-[#EEF7F0] text-[#1E6B3C]",
            error: "border-[#E9C5C1] bg-[#FFF0EE] text-[#A52B20]",
        };
        message.className = `mt-3 rounded-[8px] border px-4 py-3 text-[12px] ${colors[kind] || colors.info}`;
        message.textContent = text;
        message.classList.remove("hidden");
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
        if (!response.ok) throw new Error(result.error || "Não foi possível concluir esta etapa.");
        return result;
    }

    function attemptUrl(template, attemptId) {
        return template.replace("/0/", `/${attemptId}/`);
    }

    async function connectExpectedWallet() {
        if (!window.ethereum) throw new Error("Abra esta página em um navegador com MetaMask ou outra carteira EVM.");
        const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
        const wallet = String(accounts?.[0] || "").toLowerCase();
        if (!wallet) throw new Error("Nenhuma carteira foi conectada.");
        if (wallet !== expectedWallet) {
            throw new Error("A carteira conectada não é a carteira indicada no termo. Troque para a carteira do participante.");
        }
        const chainId = Number.parseInt(await window.ethereum.request({ method: "eth_chainId" }), 16);
        if (chainId !== expectedChain) {
            throw new Error(`A carteira está na rede ${chainId}. Troque para a rede da proposta (ID ${expectedChain}).`);
        }
        return { wallet, chainId };
    }

    function addHashRecovery(url, hash) {
        recovery.replaceChildren();
        const form = document.createElement("form");
        form.className = "flex flex-wrap gap-2";
        form.innerHTML = `<input name="transaction_hash" class="min-w-[220px] flex-1 rounded-[7px] border border-[#D7DDD4] px-3 py-2 font-mono text-[10px]" aria-label="Hash da transação" required><button class="min-h-[40px] rounded-[7px] bg-[#1E6B3C] px-4 text-[11px] font-semibold text-white">Tentar registrar esta hash</button>`;
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
        recovery.appendChild(form);
    }

    async function sendPrepared(attempt, attemptId, button) {
        const startedUrl = attemptUrl(root.dataset.startTemplate, attemptId);
        const hashUrl = attemptUrl(root.dataset.hashTemplate, attemptId);
        const rejectedUrl = attemptUrl(root.dataset.rejectedTemplate, attemptId);
        const started = await postJson(startedUrl, {});
        if (!started.send_allowed) {
            showMessage("Esta tentativa já abriu a carteira. Se a resposta foi perdida, informe a hash da mesma tentativa.", "error");
            window.location.reload();
            return;
        }

        let hash = "";
        try {
            hash = await window.ethereum.request({
                method: "eth_sendTransaction",
                params: [{
                    from: attempt.from,
                    to: attempt.to,
                    data: attempt.data,
                    value: "0x0",
                }],
            });
        } catch (error) {
            if (error?.code === 4001) {
                await postJson(rejectedUrl, {});
                showMessage("Você recusou o envio. O termo não foi confirmado; você poderá assinar novamente.", "info");
            } else {
                showMessage("A carteira não respondeu com certeza. Não inicie outro envio. Se apareceu uma hash, informe-a abaixo.", "error");
                window.location.reload();
            }
            return;
        }

        try {
            await postJson(hashUrl, { transaction_hash: hash });
            showMessage("Hash registrada. Aguarde a rede e consulte a confirmação.", "success");
            window.location.reload();
        } catch (error) {
            showMessage(`${error.message} A hash que a carteira retornou ficou disponível para recuperar esta tentativa.`, "error");
            addHashRecovery(hashUrl, hash);
        }
        void button;
    }

    document.querySelector("#sign-acceptance")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const account = await connectExpectedWallet();
            const challenge = await postJson(root.dataset.challengeUrl, {
                wallet_address: account.wallet,
                wallet_chain_id: account.chainId,
            });
            let signature;
            try {
                signature = await window.ethereum.request({
                    method: "eth_signTypedData_v4",
                    params: [account.wallet, JSON.stringify(challenge.typed_data)],
                });
            } catch (error) {
                if (error?.code === 4001) {
                    showMessage("Você recusou a assinatura da mensagem. Nenhuma transação foi enviada; você pode tentar novamente.", "info");
                    return;
                }
                throw error;
            }

            // The server checks this EIP-712 signature and saves the transaction
            // before the second MetaMask prompt asks to broadcast it.
            const prepared = await postJson(root.dataset.prepareUrl, {
                challenge_id: challenge.challenge_id,
                signature,
                wallet_address: account.wallet,
                wallet_chain_id: account.chainId,
            });
            await sendPrepared(
                { from: prepared.from, to: prepared.to, data: prepared.data },
                prepared.attempt_id,
                button,
            );
        } catch (error) {
            showMessage(error.message, "error");
        } finally {
            button.disabled = false;
        }
    });

    document.querySelector("#resume-acceptance")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const account = await connectExpectedWallet();
            await sendPrepared(
                { from: account.wallet, to: button.dataset.to, data: button.dataset.data },
                Number(button.dataset.attemptId),
                button,
            );
        } catch (error) {
            showMessage(error.message, "error");
        } finally {
            button.disabled = false;
        }
    });

    document.querySelectorAll(".acceptance-hash-form").forEach((form) => {
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

    document.querySelectorAll(".acceptance-confirm-form").forEach((form) => {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = form.querySelector("button");
            button.disabled = true;
            try {
                const result = await postJson(form.dataset.url, {});
                showMessage(result.message, result.status === "confirmed" ? "success" : "info");
                window.setTimeout(() => window.location.reload(), 1200);
            } catch (error) {
                showMessage(error.message, "error");
                button.disabled = false;
            }
        });
    });
})();
