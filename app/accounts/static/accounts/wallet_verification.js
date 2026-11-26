// Connect MetaMask and submit an off-chain wallet ownership signature.

(() => {
    const panel = document.querySelector("[data-wallet-linking]");
    if (!panel) return;

    const connectButton = panel.querySelector("[data-wallet-connect]");
    const networkSelect = panel.querySelector("[data-wallet-network]");
    const messageElement = panel.querySelector("[data-wallet-message]");
    const csrfInput = panel.querySelector('input[name="csrfmiddlewaretoken"]');

    const showMessage = (message, isError = false) => {
        messageElement.textContent = message;
        messageElement.classList.toggle("text-red-700", isError);
        messageElement.classList.toggle("text-[#1E6B3C]", !isError);
    };

    const postJson = async (url, payload) => {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfInput.value,
            },
            body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Não foi possível concluir a operação.");
        return result;
    };

    const toHexMessage = (message) => {
        const bytes = new TextEncoder().encode(message);
        return `0x${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
    };

    connectButton.addEventListener("click", async () => {
        if (!window.ethereum) {
            showMessage("MetaMask não encontrada. Instale ou habilite a extensão no navegador.", true);
            return;
        }

        const requestedChainId = Number(networkSelect.value);
        if (!requestedChainId) {
            showMessage("Selecione uma rede ativa.", true);
            return;
        }

        connectButton.disabled = true;
        try {
            showMessage("Conecte a carteira e confira a rede selecionada.");
            const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
            const address = accounts[0];
            if (!address) throw new Error("Nenhuma conta foi selecionada na carteira.");

            const currentChainId = Number.parseInt(
                await window.ethereum.request({ method: "eth_chainId" }),
                16,
            );
            if (currentChainId !== requestedChainId) {
                throw new Error(`Troque a MetaMask para a rede selecionada (ID ${requestedChainId}).`);
            }

            showMessage("Preparando a confirmação de propriedade da carteira.");
            const challenge = await postJson(panel.dataset.challengeUrl, {
                address,
                chain_id: requestedChainId,
            });

            showMessage("Confira a mensagem na MetaMask e assine para vincular a carteira.");
            const signature = await window.ethereum.request({
                method: "personal_sign",
                params: [toHexMessage(challenge.message), challenge.address],
            });

            showMessage("Verificando a assinatura.");
            await postJson(panel.dataset.verifyUrl, {
                challenge_id: challenge.challenge_id,
                signature,
            });
            showMessage("Carteira verificada. Atualizando a página.");
            window.location.reload();
        } catch (error) {
            if (error && error.code === 4001) {
                showMessage("Você recusou a solicitação na MetaMask. Nenhuma transação foi enviada.", true);
            } else {
                showMessage(error.message || "Não foi possível verificar a carteira.", true);
            }
        } finally {
            connectButton.disabled = false;
        }
    });
})();
