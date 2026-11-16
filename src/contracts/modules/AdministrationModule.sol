// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";

// Administração do contrato: pausa, armazém, auditor, participantes e lastro.
abstract contract AdministrationModule is TokenModule {
    /// @notice Pausa as movimentações de SJC.
    /// @dev Só o dono pode chamar. A primeira pausa registra o horário usado
    /// depois para exigir uma auditoria mais recente antes de reativar o contrato.
    function pause() external onlyOwner {
        _pausarContrato();
    }

    /// @notice Reativa o contrato depois de uma auditoria recente que confirme lastro suficiente.
    /// @dev Só o dono pode chamar. Exige auditoria dentro do prazo, posterior à última pausa,
    /// e estoque registrado suficiente para cobrir o total de SJC emitido.
    function unpause() external onlyOwner {
        require(ultimaAuditoria != 0, "Auditoria inexistente");
        require(
            block.timestamp <= ultimaAuditoria + PRAZO_AUDITORIA,
            "Auditoria vencida"
        );
        require(ultimaAuditoria >= ultimaPausa, "Auditoria anterior a pausa");
        require(
            totalSupply() * MG_POR_UNIDADE_SJC <= estoqueRegistradoMg,
            "Lastro insuficiente"
        );
        _unpause();
    }

    /// @notice Propõe uma nova carteira para operar como armazém.
    /// @dev Só o dono pode chamar. A troca só se efetiva quando a carteira proposta aceita.
    /// O endereço não pode ser zero, auditor, participante ou o armazém atual.
    /// @param novoArmazem Carteira que receberá a proposta de assumir o papel de armazém.
    function proporNovoArmazem(address novoArmazem) external onlyOwner {
        require(novoArmazem != address(0), "Armazem invalido");
        require(novoArmazem != wareHouse, "Armazem ja configurado");
        require(novoArmazem != auditor, "Armazem nao pode ser auditor");
        require(
            !participanteCadastrado[novoArmazem],
            "Armazem nao pode ser participante"
        );

        pendingWareHouse = novoArmazem;
        emit NovoArmazemProposto(wareHouse, novoArmazem, msg.sender);
    }

    /// @notice Aceita a proposta para se tornar a carteira operacional do armazém.
    /// @dev Só a carteira atualmente proposta pode chamar. Ao aceitar, substitui o armazém
    /// atual e limpa a proposta pendente.
    function aceitarArmazem() external {
        require(msg.sender == pendingWareHouse, "Apenas armazem pendente");
        require(msg.sender != auditor, "Armazem nao pode ser auditor");
        require(
            !participanteCadastrado[msg.sender],
            "Armazem nao pode ser participante"
        );

        address armazemAnterior = wareHouse;
        wareHouse = pendingWareHouse;
        pendingWareHouse = address(0);

        emit ArmazemAlterado(armazemAnterior, wareHouse);
    }

    /// @notice Define a carteira que pode registrar auditorias de estoque.
    /// @dev Só o dono pode chamar. A carteira precisa ser diferente de zero, do armazém,
    /// do armazém pendente e de qualquer participante cadastrado.
    /// @param walletAddress Carteira que será definida como auditor.
    function cadastrarAuditor(address walletAddress) external onlyOwner {
        require(walletAddress != address(0), "Auditor invalido");
        require(walletAddress != wareHouse, "Auditor nao pode ser o armazem");
        require(
            walletAddress != pendingWareHouse,
            "Auditor nao pode ser armazem pendente"
        );
        require(
            !participanteCadastrado[walletAddress],
            "Auditor nao pode ser um dos participantes do contrato"
        );

        auditor = walletAddress;
        emit AuditorCadastrado(auditor, msg.sender, block.timestamp);
    }

    /// @notice Registra o estoque físico contado pelo auditor e atualiza o teto de emissão.
    /// @dev Só o auditor cadastrado pode chamar. O estoque informado em kg é convertido
    /// para miligramas; se o estoque não cobrir os SJC emitidos, o contrato é pausado.
    /// @param estoqueFisicoKg Quantidade física total contada, em quilogramas inteiros.
    /// @param hashDoLaudo Hash do laudo de auditoria armazenado fora da blockchain.
    function RegistrarAuditoria(
        uint256 estoqueFisicoKg,
        bytes32 hashDoLaudo
    ) external {
        require(
            msg.sender == auditor,
            "Apenas o auditor cadastrado pode realizar a auditoria"
        );
        require(hashDoLaudo != bytes32(0), "Hash do laudo invalida");

        estoqueRegistradoMg = estoqueFisicoKg * MG_POR_KG;
        _atualizarTetoEmissao();
        ultimaAuditoria = block.timestamp;

        emit AuditoriaRegistrada(
            msg.sender,
            wareHouse,
            estoqueFisicoKg,
            totalSupply(),
            hashDoLaudo,
            block.timestamp
        );

        _pausarSeSemLastro(msg.sender);
    }

    /// @notice Cadastra uma carteira como participante autorizado a operar com SJC.
    /// @dev Só o dono ou o armazém pode chamar. A carteira não pode ser zero, auditor,
    /// armazém atual, armazém pendente ou participante já cadastrado.
    /// @param walletAddress Carteira do produtor ou credor que será cadastrada.
    /// @param hashTermoDeAdesao Hash do termo de adesão guardado fora da blockchain.
    function cadastrarParticipante(
        address walletAddress,
        bytes32 hashTermoDeAdesao
    ) external {
        require(
            msg.sender == owner() || msg.sender == wareHouse,
            "Somente dono ou armazem"
        );
        require(walletAddress != address(0), "Endereco invalido");
        require(walletAddress != auditor, "Auditor nao pode ser participante");
        require(walletAddress != wareHouse, "Armazem nao pode ser participante");
        require(
            walletAddress != pendingWareHouse,
            "Armazem pendente nao pode ser participante"
        );
        require(
            !participanteCadastrado[walletAddress],
            "Participante ja cadastrado"
        );

        participanteCadastrado[walletAddress] = true;
        emit ParticipanteCadastrado(walletAddress, hashTermoDeAdesao, msg.sender);
    }

    /// @notice Remove o cadastro de um participante sem operações ou saldos pendentes.
    /// @dev Só o dono pode chamar. Exige saldo SJC zero e ausência de depósitos pendentes,
    /// resgates, travas e CPRs ativos para a carteira.
    /// @param walletAddress Carteira do participante a remover.
    function removerParticipante(address walletAddress) external onlyOwner {
        require(
            participanteCadastrado[walletAddress],
            "Participante nao cadastrado"
        );
        require(
            depositosPendentes[walletAddress] == 0,
            "Participante possui depositos pendentes"
        );
        require(balanceOf(walletAddress) == 0, "Participante possui SJC");
        require(
            resgatesPendentesPorHolder[walletAddress] == 0,
            "Participante possui resgates pendentes"
        );
        require(
            travasAtivasPorParticipante[walletAddress] == 0,
            "Participante possui travas ativas"
        );
        require(
            cprsAtivasPorParticipante[walletAddress] == 0,
            "Participante possui CPRs ativas"
        );

        participanteCadastrado[walletAddress] = false;
        emit ParticipanteRemovido(walletAddress);
    }
}
