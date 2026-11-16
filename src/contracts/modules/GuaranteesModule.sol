// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {TokenModule} from "./TokenModule.sol";

// CPRs que reservam emissões futuras de SJC para um credor, em ordem de prioridade.
abstract contract GuaranteesModule is TokenModule {
    /// @notice Consulta os IDs das CPRs ainda em andamento de um produtor, na ordem de prioridade.
    /// @param produtor Carteira do produtor cuja fila será consultada.
    /// @return idsCPR IDs das CPRs ativas ou cobertas que ainda não foram encerradas.
    function cprsAtivasDoProdutor(
        address produtor
    ) external view returns (uint256[] memory) {
        return _cprsAtivasDoProdutor[produtor];
    }

    /// @notice Calcula a mensagem digital que o produtor precisa assinar para autorizar uma CPR.
    /// @dev Usa o nonce atual do produtor. A chamada apenas calcula o digest e não grava dados.
    /// @param produtor Carteira que dará a autorização.
    /// @param credor Carteira que poderá receber os SJC reservados, se a CPR for executada.
    /// @param valorGarantidoSc Valor da garantia em unidades internas do SJC (seis casas decimais).
    /// @param ordemExecucao Posição desta CPR na fila de prioridade do produtor.
    /// @param vencimento Data, em segundos Unix, a partir da qual o credor pode executar a garantia.
    /// @param hashCPR Hash do documento da CPR guardado fora da blockchain.
    /// @param deadline Data, em segundos Unix, até a qual a assinatura pode ser usada.
    /// @return digest Mensagem EIP-712 que deve ser assinada pelo produtor.
    function hashAutorizacaoCPRGarantia(
        address produtor,
        address credor,
        uint256 valorGarantidoSc,
        uint256 ordemExecucao,
        uint256 vencimento,
        bytes32 hashCPR,
        uint256 deadline
    ) external view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                AUTORIZACAO_CPR_TYPEHASH,
                produtor,
                credor,
                valorGarantidoSc,
                ordemExecucao,
                vencimento,
                hashCPR,
                noncesCPR[produtor],
                deadline
            )
        );
        return _hashTypedDataV4(structHash);
    }

    /// @notice Registra uma CPR que reservará emissões futuras de SJC para o credor.
    /// @dev Só o dono ou o armazém pode enviar a transação. O produtor também precisa
    /// assinar os mesmos dados retornados por {hashAutorizacaoCPRGarantia}.
    /// A ordem precisa ser crescente e a assinatura só pode ser usada até o deadline.
    /// @param produtor Carteira do produtor que autoriza vincular futuras emissões.
    /// @param credor Participante que poderá liberar ou executar os SJC reservados.
    /// @param valorGarantidoSc Valor-alvo da garantia em unidades internas do SJC.
    /// @param ordemExecucao Prioridade desta CPR em relação às outras do produtor.
    /// @param vencimento Data Unix de vencimento da CPR.
    /// @param hashCPR Hash do documento da CPR guardado fora da blockchain.
    /// @param deadline Prazo Unix da autorização assinada pelo produtor.
    /// @param assinaturaProdutor Assinatura EIP-712 do produtor sobre esses dados.
    /// @return idCPR Identificador atribuído à CPR registrada.
    function registrarCPRGarantia(
        address produtor,
        address credor,
        uint256 valorGarantidoSc,
        uint256 ordemExecucao,
        uint256 vencimento,
        bytes32 hashCPR,
        uint256 deadline,
        bytes calldata assinaturaProdutor
    ) external returns (uint256 idCPR) {
        require(
            msg.sender == owner() || msg.sender == wareHouse,
            "Somente dono ou armazem"
        );
        require(participanteCadastrado[produtor], "Produtor nao cadastrado");
        require(participanteCadastrado[credor], "Credor nao cadastrado");
        require(produtor != credor, "Produtor e credor iguais");
        require(valorGarantidoSc > 0, "Valor da CPR invalido");
        require(vencimento > block.timestamp, "Vencimento invalido");
        require(hashCPR != bytes32(0), "Hash da CPR invalida");
        require(block.timestamp <= deadline, "Autorizacao expirada");
        require(
            _cprsAtivasDoProdutor[produtor].length <
                MAX_CPRS_ATIVAS_POR_PRODUTOR,
            "Limite de CPRs ativas"
        );
        require(
            ordemExecucao > ultimaOrdemCPRPorProdutor[produtor],
            "Ordem da CPR invalida"
        );

        uint256 nonce = noncesCPR[produtor];
        bytes32 structHash = keccak256(
            abi.encode(
                AUTORIZACAO_CPR_TYPEHASH,
                produtor,
                credor,
                valorGarantidoSc,
                ordemExecucao,
                vencimento,
                hashCPR,
                nonce,
                deadline
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        require(
            SignatureChecker.isValidSignatureNow(produtor, digest, assinaturaProdutor),
            "Autorizacao do produtor invalida"
        );

        noncesCPR[produtor] = nonce + 1;
        ultimaOrdemCPRPorProdutor[produtor] = ordemExecucao;

        idCPR = ++proximaCPRGarantia;
        cprsGarantia[idCPR] = CPRGarantia(
            produtor,
            credor,
            valorGarantidoSc,
            0,
            ordemExecucao,
            vencimento,
            hashCPR,
            StatusCPRGarantia.Ativa
        );
        _cprsAtivasDoProdutor[produtor].push(idCPR);
        cprsAtivasPorParticipante[produtor] += 1;
        cprsAtivasPorParticipante[credor] += 1;

        emit CPRGarantiaRegistrada(
            idCPR,
            produtor,
            credor,
            valorGarantidoSc,
            ordemExecucao,
            hashCPR,
            vencimento
        );
    }

    /// @notice Encerra a CPR como resolvida e devolve ao produtor os SJC reservados.
    /// @dev Só o credor registrado pode chamar. A blockchain não verifica a quitação
    /// externa: o credor registra que a obrigação foi resolvida.
    /// @param idCPR Identificador da CPR a liberar.
    function liberarCPRGarantia(uint256 idCPR) external {
        CPRGarantia storage cpr = cprsGarantia[idCPR];
        require(
            cpr.status == StatusCPRGarantia.Ativa ||
                cpr.status == StatusCPRGarantia.Coberta,
            "CPR nao esta ativa"
        );
        require(msg.sender == cpr.credor, "Apenas o credor");

        uint256 amountSc = cpr.bloqueadoSc;
        cpr.bloqueadoSc = 0;
        cpr.status = StatusCPRGarantia.Liberada;
        _encerrarCPRGarantia(idCPR, cpr.produtor, cpr.credor);

        if (amountSc > 0) {
            _transferirTokensDaTrava(address(this), cpr.produtor, amountSc);
        }

        emit CPRGarantiaLiberada(idCPR, cpr.produtor, amountSc);
    }

    /// @notice Cancela uma CPR que ainda não recebeu nenhum SJC reservado.
    /// @dev Só o dono pode chamar, e a CPR precisa estar ativa com saldo bloqueado igual a zero.
    /// CPRs que já receberam SJC não podem ser canceladas por esta função.
    /// @param idCPR Identificador da CPR a cancelar.
    function cancelarCPRGarantia(uint256 idCPR) external onlyOwner {
        CPRGarantia storage cpr = cprsGarantia[idCPR];
        require(cpr.status == StatusCPRGarantia.Ativa, "CPR nao esta ativa");
        require(cpr.bloqueadoSc == 0, "CPR possui SJC bloqueados");

        cpr.status = StatusCPRGarantia.Cancelada;
        _encerrarCPRGarantia(idCPR, cpr.produtor, cpr.credor);

        emit CPRGarantiaCancelada(idCPR);
    }

    /// @notice Transfere ao credor os SJC que já foram reservados para a CPR vencida.
    /// @dev Só o credor registrado pode chamar a partir do vencimento. A função transfere
    /// o valor efetivamente reservado, que pode ser menor que o valor-alvo da CPR.
    /// @param idCPR Identificador da CPR a executar.
    function executarCPRGarantia(uint256 idCPR) external {
        CPRGarantia storage cpr = cprsGarantia[idCPR];
        require(
            cpr.status == StatusCPRGarantia.Ativa ||
                cpr.status == StatusCPRGarantia.Coberta,
            "CPR nao esta ativa"
        );
        require(msg.sender == cpr.credor, "Apenas o credor");
        require(block.timestamp >= cpr.vencimento, "Ainda nao venceu");
        require(cpr.bloqueadoSc > 0, "CPR sem SJC bloqueados");

        uint256 amountSc = cpr.bloqueadoSc;
        cpr.bloqueadoSc = 0;
        cpr.status = StatusCPRGarantia.Executada;
        _encerrarCPRGarantia(idCPR, cpr.produtor, cpr.credor);

        _transferirTokensDaTrava(address(this), cpr.credor, amountSc);
        emit CPRGarantiaExecutada(idCPR, cpr.credor, amountSc);
    }

    /// @notice Resolve uma CPR sem ação do credor após o vencimento e a carência de 30 dias.
    /// @dev Só o dono pode chamar. Se entregarAoCredor for true, os SJC reservados vão ao
    /// credor; se for false, voltam ao produtor. Em ambos os casos a CPR é encerrada.
    /// @param idCPR Identificador da CPR a resolver.
    /// @param entregarAoCredor Define se o saldo reservado será enviado ao credor ou ao produtor.
    function resolverCPRComoArbitro(
        uint256 idCPR,
        bool entregarAoCredor
    ) external onlyOwner {
        CPRGarantia storage cpr = cprsGarantia[idCPR];
        require(
            cpr.status == StatusCPRGarantia.Ativa ||
                cpr.status == StatusCPRGarantia.Coberta,
            "CPR nao esta ativa"
        );
        require(
            block.timestamp >= cpr.vencimento + CARENCIA_CPR,
            "Carencia ainda nao venceu"
        );

        uint256 amountSc = cpr.bloqueadoSc;
        cpr.bloqueadoSc = 0;

        if (entregarAoCredor) {
            cpr.status = StatusCPRGarantia.Executada;
            _encerrarCPRGarantia(idCPR, cpr.produtor, cpr.credor);
            if (amountSc > 0) {
                _transferirTokensDaTrava(address(this), cpr.credor, amountSc);
            }
        } else {
            cpr.status = StatusCPRGarantia.Liberada;
            _encerrarCPRGarantia(idCPR, cpr.produtor, cpr.credor);
            if (amountSc > 0) {
                _transferirTokensDaTrava(address(this), cpr.produtor, amountSc);
            }
        }

        emit CPRGarantiaArbitrada(idCPR, entregarAoCredor, amountSc);
    }

    /// @notice Remove uma CPR encerrada da fila do produtor e atualiza os contadores ativos.
    /// @dev Função interna usada após liberar, cancelar, executar ou arbitrar uma CPR.
    /// @param idCPR Identificador da CPR removida da fila.
    /// @param produtor Carteira do produtor dono da fila.
    /// @param credor Carteira do credor cujos contadores ativos serão atualizados.
    function _encerrarCPRGarantia(
        uint256 idCPR,
        address produtor,
        address credor
    ) private {
        uint256[] storage fila = _cprsAtivasDoProdutor[produtor];
        for (uint256 i = 0; i < fila.length; i++) {
            if (fila[i] == idCPR) {
                for (uint256 j = i; j + 1 < fila.length; j++) {
                    fila[j] = fila[j + 1];
                }
                fila.pop();
                cprsAtivasPorParticipante[produtor] -= 1;
                cprsAtivasPorParticipante[credor] -= 1;
                return;
            }
        }
        revert("CPR nao encontrada na fila");
    }

    /// @notice Emite SJC diretamente para o contrato mantê-los reservados à CPR.
    /// @dev A flag temporária permite que o contrato receba esses tokens; não os envia ao produtor.
    /// @param amountSc Quantidade emitida em unidades internas do SJC.
    function _mintParaCPRGarantia(uint256 amountSc) private {
        _movimentacaoDeTrava = true;
        _mint(address(this), amountSc);
        _movimentacaoDeTrava = false;
    }

    /// @notice Reserva parte da emissão de um depósito para as CPRs do produtor, em ordem de prioridade.
    /// @dev Chamado durante a emissão. Cada CPR ativa recebe até atingir seu valor-alvo;
    /// a função retorna a sobra que pode ser emitida livremente ao produtor.
    /// @param produtor Carteira do produtor que receberia a emissão.
    /// @param idDeposit Depósito que gerou esta emissão, usado no evento de reserva.
    /// @param amountSc Quantidade total emitida pelo depósito, em unidades internas do SJC.
    /// @return amountLivreSc Parte da emissão que não precisou ficar reservada para CPRs.
    function _reservarParaCPRs(
        address produtor,
        uint256 idDeposit,
        uint256 amountSc
    ) internal returns (uint256 amountLivreSc) {
        uint256 restante = amountSc;
        uint256[] storage fila = _cprsAtivasDoProdutor[produtor];

        for (uint256 i = 0; i < fila.length && restante > 0; i++) {
            CPRGarantia storage cpr = cprsGarantia[fila[i]];
            if (cpr.status != StatusCPRGarantia.Ativa) {
                continue;
            }

            uint256 faltaCobrir = cpr.valorGarantidoSc - cpr.bloqueadoSc;
            uint256 amountBloqueado = restante < faltaCobrir
                ? restante
                : faltaCobrir;

            _mintParaCPRGarantia(amountBloqueado);
            cpr.bloqueadoSc += amountBloqueado;
            restante -= amountBloqueado;

            if (cpr.bloqueadoSc == cpr.valorGarantidoSc) {
                cpr.status = StatusCPRGarantia.Coberta;
            }

            emit CPRGarantiaBloqueada(
                fila[i],
                idDeposit,
                amountBloqueado,
                cpr.bloqueadoSc
            );
        }

        return restante;
    }
}
