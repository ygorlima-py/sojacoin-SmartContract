// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";

// Serviços cobrados pelo armazém sem alterar o peso aceito no romaneio.
abstract contract ServicesModule is TokenModule {
    /// @notice Consulta os IDs das cobranças de serviços ligadas a um depósito.
    /// @dev Esta consulta apenas lê a lista registrada para o depósito.
    /// @param idDeposit Identificador do depósito consultado.
    /// @return idsServicoETarifa IDs das cobranças associadas ao depósito.
    function servicosETarifasDoDeposito(
        uint256 idDeposit
    ) external view returns (uint256[] memory) {
        return _servicosETarifasDoDeposito[idDeposit];
    }

    /// @notice Registra uma cobrança de serviço vinculada a um depósito já emitido.
    /// @dev Só o armazém pode chamar. O recebedor deve ser outro participante cadastrado;
    /// a cobrança fica aberta para pagamento em SJC ou confirmação de pagamento em reais.
    /// O hash identifica a cobrança detalhada guardada fora da blockchain.
    /// @param idDeposit Depósito que originou o serviço.
    /// @param recebedor Participante que receberá o pagamento em SJC, se essa unidade for escolhida.
    /// @param tipo Categoria do serviço prestado.
    /// @param unidade Indica se o valor está em SJC ou em centavos de reais.
    /// @param valor Valor da cobrança na unidade indicada.
    /// @param hashCobranca Hash do documento que detalha a cobrança.
    /// @return idServicoETarifa Identificador da cobrança registrada.
    function registrarServicoETarifa(
        uint256 idDeposit,
        address recebedor,
        TipoServico tipo,
        UnidadeTarifa unidade,
        uint256 valor,
        bytes32 hashCobranca
    ) external returns (uint256 idServicoETarifa) {
        require(msg.sender == wareHouse, "So o armazem");

        Deposit storage deposit = deposits[idDeposit];
        require(deposit.depositante != address(0), "Deposito inexistente");
        require(deposit.tokensIssued, "Tokens do deposito nao emitidos");
        require(participanteCadastrado[recebedor], "Recebedor nao cadastrado");
        require(recebedor != deposit.depositante, "Recebedor igual ao produtor");
        require(valor > 0, "Valor invalido");
        require(hashCobranca != bytes32(0), "Hash da cobranca invalida");

        idServicoETarifa = ++proximoServicoETarifa;
        servicosETarifas[idServicoETarifa] = ServicoETarifa(
            idDeposit,
            deposit.depositante,
            recebedor,
            tipo,
            unidade,
            valor,
            hashCobranca,
            bytes32(0),
            StatusServicoETarifa.Aberto
        );
        _servicosETarifasDoDeposito[idDeposit].push(idServicoETarifa);

        emit ServicoETarifaRegistrado(
            idServicoETarifa,
            idDeposit,
            deposit.depositante,
            recebedor,
            tipo,
            unidade,
            valor,
            hashCobranca
        );
    }

    /// @notice Paga em SJC uma cobrança aberta cuja unidade seja SJC.
    /// @dev Só o produtor vinculado à cobrança pode chamar. Transfere o valor ao recebedor
    /// registrado e muda o estado da cobrança para PagoEmSJC.
    /// @param idServicoETarifa Identificador da cobrança a pagar.
    function pagarServicoEmSJC(uint256 idServicoETarifa) external {
        ServicoETarifa storage servico = servicosETarifas[idServicoETarifa];

        require(
            servico.status == StatusServicoETarifa.Aberto,
            "Servico nao esta aberto"
        );
        require(msg.sender == servico.produtor, "Apenas o produtor");
        require(
            servico.unidade == UnidadeTarifa.SJC,
            "Servico nao e cobrado em SJC"
        );

        servico.status = StatusServicoETarifa.PagoEmSJC;
        _transfer(msg.sender, servico.recebedor, servico.valor);

        emit ServicoPagoEmSJC(
            idServicoETarifa,
            msg.sender,
            servico.recebedor,
            servico.valor
        );
    }

    /// @notice Registra que uma cobrança em reais foi paga fora da blockchain.
    /// @dev Só o armazém pode chamar. O contrato guarda a hash do comprovante informado
    /// pelo armazém; não consulta nem confirma diretamente uma conta bancária.
    /// @param idServicoETarifa Identificador da cobrança em reais.
    /// @param hashComprovantePagamento Hash do comprovante guardado fora da blockchain.
    function confirmarServicoPagoEmReais(
        uint256 idServicoETarifa,
        bytes32 hashComprovantePagamento
    ) external {
        require(msg.sender == wareHouse, "So o armazem");

        ServicoETarifa storage servico = servicosETarifas[idServicoETarifa];
        require(
            servico.status == StatusServicoETarifa.Aberto,
            "Servico nao esta aberto"
        );
        require(
            servico.unidade == UnidadeTarifa.ReaisCentavos,
            "Servico nao e cobrado em reais"
        );
        require(
            hashComprovantePagamento != bytes32(0),
            "Hash do comprovante invalida"
        );

        servico.hashComprovantePagamento = hashComprovantePagamento;
        servico.status = StatusServicoETarifa.PagoEmReais;

        emit ServicoPagoEmReais(idServicoETarifa, hashComprovantePagamento);
    }

    /// @notice Cancela uma cobrança de serviço que ainda está aberta.
    /// @dev Só o armazém pode chamar. Uma cobrança paga não pode ser cancelada por este método.
    /// @param idServicoETarifa Identificador da cobrança a cancelar.
    function cancelarServicoETarifa(uint256 idServicoETarifa) external {
        require(msg.sender == wareHouse, "So o armazem");

        ServicoETarifa storage servico = servicosETarifas[idServicoETarifa];
        require(
            servico.status == StatusServicoETarifa.Aberto,
            "Servico nao esta aberto"
        );

        servico.status = StatusServicoETarifa.Cancelado;
        emit ServicoETarifaCancelado(idServicoETarifa);
    }
}
