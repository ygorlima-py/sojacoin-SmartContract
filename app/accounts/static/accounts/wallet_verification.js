// Connect MetaMask and submit an off-chain wallet ownership signature.

(() => {
    const panel = document.querySelector("[data-wallet-linking]");
    if (!panel) return;

    const connectButton = panel.querySelector("[data-wallet-connect]");
    const networkSelect = panel.querySelector("[data-wallet-network]");
    const messageElement = panel.querySelector("[data-wallet-message]");
    const csrfInput = panel.querySelector('input[name="csrfmiddlewaretoken"]');

    const showMessage = (message, isError = false) => {
        messageElement.textContent = gettext(message);
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
        if (!response.ok) throw new Error(result.error || gettext("Não foi possível concluir a operação."));
        return result;
    };

    const toHexMessage = (message) => {
        const bytes = new TextEncoder().encode(message);
        return `0x${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
    };

    connectButton.addEventListener("click", async () => {
        if (!window.ethereum) {
            showMessage(gettext("MetaMask não encontrada. Instale ou habilite a extensão no navegador."), true);
            return;
        }

        const requestedChainId = Number(networkSelect.value);
        if (!requestedChainId) {
            showMessage(gettext("Selecione uma rede ativa."), true);
            return;
        }

        connectButton.disabled = true;
        try {
            showMessage(gettext("Conecte a carteira e confira a rede selecionada."));
            const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
            const address = accounts[0];
            if (!address) throw new Error(gettext("Nenhuma conta foi selecionada na carteira."));

            const currentChainId = Number.parseInt(
                await window.ethereum.request({ method: "eth_chainId" }),
                16,
            );
            if (currentChainId !== requestedChainId) {
                throw new Error(interpolate(
                    gettext("Troque a MetaMask para a rede selecionada (ID %(chainId)s)."),
                    { chainId: requestedChainId },
                    true,
                ));
            }

            showMessage(gettext("Preparando a confirmação de propriedade da carteira."));
            const challenge = await postJson(panel.dataset.challengeUrl, {
                address,
                chain_id: requestedChainId,
            });

            showMessage(gettext("Confira a mensagem na MetaMask e assine para vincular a carteira."));
            const signature = await window.ethereum.request({
                method: "personal_sign",
                params: [toHexMessage(challenge.message), challenge.address],
            });

            showMessage(gettext("Verificando a assinatura."));
            await postJson(panel.dataset.verifyUrl, {
                challenge_id: challenge.challenge_id,
                signature,
            });
            showMessage(gettext("Carteira verificada. Atualizando a página."));
            window.location.reload();
        } catch (error) {
            if (error && error.code === 4001) {
                showMessage(gettext("Você recusou a solicitação na MetaMask. Nenhuma transação foi enviada."), true);
            } else {
                showMessage(error.message || gettext("Não foi possível verificar a carteira."), true);
            }
        } finally {
            connectButton.disabled = false;
        }
    });
})();
