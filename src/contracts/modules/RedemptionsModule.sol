// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";

// Pedidos de retirada física, autorização do armazém e arbitragem do dono.
abstract contract RedemptionsModule is TokenModule {
    /// @notice Solicita a retirada física de soja e guarda os SJC do pedido no contrato.
    /// @dev Só participante cadastrado pode chamar. A solicitação não autoriza a saída:
    /// o armazém precisa aprová-la depois.
    /// @param amountSc Quantidade de SJC a resgatar, em unidades internas do token.
    /// @return idResgate Identificador atribuído ao pedido.
    function solicitarResgate(
        uint256 amountSc
    ) external returns (uint256 idResgate) {
        require(
            participanteCadastrado[msg.sender],
            "Solicitante nao cadastrado"
        );
        require(amountSc > 0, "Quantidade invalida");

        idResgate = ++proximoResgate;
        resgates[idResgate] = Resgate(
            msg.sender,
            amountSc,
            StatusResgate.Solicitado,
            0
        );
        resgatesPendentesPorHolder[msg.sender] += 1;

        _transferirTokensDaTrava(msg.sender, address(this), amountSc);
        emit ResgateSolicitado(idResgate, msg.sender, amountSc);
    }

    /// @notice Cancela um pedido de retirada que ainda não foi autorizado.
    /// @dev Pode ser chamado pelo titular ou pelo armazém. Os SJC mantidos pelo contrato
    /// são devolvidos ao titular, inclusive quando a pausa global está ativa.
    /// @param idResgate Identificador do pedido a cancelar.
    function cancelarSolicitacao(uint256 idResgate) external {
        Resgate storage r = resgates[idResgate];
        require(
            r.status == StatusResgate.Solicitado,
            "Resgate nao esta solicitado"
        );
        require(
            msg.sender == r.holder || msg.sender == wareHouse,
            "Nao autorizado"
        );

        r.status = StatusResgate.Cancelado;
        resgatesPendentesPorHolder[r.holder] -= 1;

        _transferirTokensDoResgate(r.holder, r.amountSc);
        emit ResgateCancelado(idResgate);
    }

    /// @notice Autoriza um pedido de retirada e contabiliza seus SJC como saída pendente.
    /// @dev Só o armazém pode chamar. Exige contrato ativo, auditoria recente e espaço
    /// suficiente no teto de estoque, considerando as retiradas já autorizadas.
    /// @param idResgate Identificador do pedido solicitado pelo titular.
    function autorizarRetirada(uint256 idResgate) external {
        require(msg.sender == wareHouse, "So o armazem");
        require(!paused(), "Contrato pausado");
        require(ultimaAuditoria != 0, "Auditoria inexistente");
        require(
            block.timestamp <= ultimaAuditoria + PRAZO_AUDITORIA,
            "Auditoria vencida"
        );

        Resgate storage r = resgates[idResgate];
        require(
            r.status == StatusResgate.Solicitado,
            "Resgate nao esta solicitado"
        );
        require(
            sacasEmRetirada + r.amountSc <= tetoEmissaoSc,
            "Estoque insuficiente para retirada"
        );

        r.status = StatusResgate.Autorizado;
        r.autorizadoEm = block.timestamp;
        sacasEmRetirada += r.amountSc;

        emit RetiradaAutorizada(idResgate, r.amountSc);
    }

    /// @notice Confirma a entrega da soja ao titular e conclui o resgate.
    /// @dev Só o armazém pode chamar. Exige hash da nota fiscal de retorno; os SJC presos
    /// são queimados e o estoque registrado é reduzido pela quantidade entregue.
    /// @param idResgate Identificador do resgate autorizado.
    /// @param hashNFRetorno Hash da nota fiscal que registra a saída da soja.
    function concluirRetirada(
        uint256 idResgate,
        bytes32 hashNFRetorno
    ) external {
        require(msg.sender == wareHouse, "So o armazem");
        _concluir(idResgate, hashNFRetorno);
    }

    /// @notice Cancela uma retirada autorizada quando a soja não foi entregue.
    /// @dev Só o armazém pode chamar. Libera a reserva pendente e devolve os SJC ao titular;
    /// como não houve entrega, não reduz o estoque registrado.
    /// @param idResgate Identificador da retirada autorizada a cancelar.
    function cancelarRetiradaAutorizada(uint256 idResgate) external {
        require(msg.sender == wareHouse, "So o armazem");
        _cancelarAutorizada(idResgate);
    }

    /// @notice Resolve uma retirada autorizada que ficou sem resposta por sete dias.
    /// @dev Só o dono pode chamar após o prazo. Se sojaFoiEntregue for true, conclui o resgate
    /// e exige um hash válido da nota; se for false, cancela e devolve os SJC ao titular.
    /// @param idResgate Identificador da retirada a arbitrar.
    /// @param sojaFoiEntregue Informa se o dono determina que a entrega física ocorreu.
    /// @param hashNFRetorno Hash da nota de retorno, usada somente se sojaFoiEntregue for true.
    function resolverComoArbitro(
        uint256 idResgate,
        bool sojaFoiEntregue,
        bytes32 hashNFRetorno
    ) external onlyOwner {
        Resgate storage r = resgates[idResgate];
        require(
            r.status == StatusResgate.Autorizado,
            "Resgate nao esta autorizado"
        );
        require(
            block.timestamp >= r.autorizadoEm + PRAZO_RETIRADA,
            "Prazo ainda nao venceu"
        );

        if (sojaFoiEntregue) {
            _concluir(idResgate, hashNFRetorno);
        } else {
            _cancelarAutorizada(idResgate);
        }
    }

    /// @notice Finaliza o resgate, reduz o estoque registrado e queima os SJC depositados no contrato.
    /// @dev Rotina privada usada pelo armazém ou pela arbitragem. Se o estoque registrado
    /// for menor que a saída, registra divergência e zera o estoque antes de verificar o lastro.
    /// @param idResgate Identificador do resgate autorizado.
    /// @param hashNFRetorno Hash da nota fiscal de retorno, que não pode ser zero.
    function _concluir(uint256 idResgate, bytes32 hashNFRetorno) private {
        require(
            hashNFRetorno != bytes32(0),
            "Hash da NF de retorno invalida"
        );

        Resgate storage r = resgates[idResgate];
        require(
            r.status == StatusResgate.Autorizado,
            "Resgate nao esta autorizado"
        );

        r.status = StatusResgate.Concluido;
        sacasEmRetirada -= r.amountSc;
        resgatesPendentesPorHolder[r.holder] -= 1;

        uint256 massaEntregueMg = r.amountSc * MG_POR_UNIDADE_SJC;
        if (estoqueRegistradoMg >= massaEntregueMg) {
            estoqueRegistradoMg -= massaEntregueMg;
        } else {
            // A retirada ainda fica registrada se a contagem estiver divergente;
            // o evento e a pausa preservam o alerta para conciliação.
            uint256 estoqueAntesDaDivergenciaMg = estoqueRegistradoMg;
            emit DivergenciaDetectada(
                msg.sender,
                wareHouse,
                estoqueAntesDaDivergenciaMg,
                totalSupply(),
                block.timestamp
            );
            estoqueRegistradoMg = 0;
        }
        _atualizarTetoEmissao();

        _queimarTokensDoResgate(r.amountSc);
        _pausarSeSemLastro(msg.sender);

        emit RetiradaConcluida(idResgate, hashNFRetorno);
    }

    /// @notice Cancela uma retirada autorizada, libera a reserva e devolve os SJC ao titular.
    /// @dev Rotina privada usada pelo armazém ou pela arbitragem quando a soja não foi entregue.
    /// Não altera o estoque físico registrado.
    /// @param idResgate Identificador do resgate autorizado a cancelar.
    function _cancelarAutorizada(uint256 idResgate) private {
        Resgate storage r = resgates[idResgate];
        require(
            r.status == StatusResgate.Autorizado,
            "Resgate nao esta autorizado"
        );

        r.status = StatusResgate.Cancelado;
        sacasEmRetirada -= r.amountSc;
        resgatesPendentesPorHolder[r.holder] -= 1;

        _transferirTokensDoResgate(r.holder, r.amountSc);
        emit ResgateCancelado(idResgate);
    }
}
