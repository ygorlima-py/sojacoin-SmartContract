// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";

// Garantias formadas por SJC já emitido e mantido no próprio contrato.
abstract contract LocksModule is TokenModule {
    /// @notice Bloqueia SJC que o titular já possui para garantir um acordo com um credor.
    /// @dev A carteira que chama é o titular. Ambos precisam estar cadastrados; os tokens
    /// saem do saldo do titular e ficam sob custódia do contrato até liberação ou execução.
    /// @param amountSc Quantidade de SJC a bloquear, em unidades internas do token.
    /// @param credor Carteira cadastrada que poderá liberar ou executar a trava.
    /// @param hashAcordo Hash do acordo guardado fora da blockchain.
    /// @param vencimento Data Unix a partir da qual o credor poderá executar a trava.
    /// @return idTrava Identificador atribuído a esta garantia.
    function travarSJC(
        uint256 amountSc,
        address credor,
        bytes32 hashAcordo,
        uint256 vencimento
    ) external returns (uint256 idTrava) {
        require(
            participanteCadastrado[msg.sender],
            "Titular nao cadastrado"
        );
        require(participanteCadastrado[credor], "Credor nao cadastrado");
        require(credor != msg.sender, "Credor igual ao titular");
        require(amountSc > 0, "Quantidade invalida");
        require(hashAcordo != bytes32(0), "Hash do acordo invalida");
        require(vencimento > block.timestamp, "Vencimento invalido");

        idTrava = ++proximaTrava;
        travas[idTrava] = Trava(
            msg.sender,
            credor,
            amountSc,
            hashAcordo,
            vencimento,
            true
        );

        _transferirTokensDaTrava(msg.sender, address(this), amountSc);

        travasAtivasPorParticipante[msg.sender] += 1;
        travasAtivasPorParticipante[credor] += 1;

        emit TokensTravados(
            idTrava,
            msg.sender,
            credor,
            amountSc,
            hashAcordo
        );
    }

    /// @notice Encerra uma trava e devolve ao titular os SJC que estavam bloqueados.
    /// @dev Só o credor da trava pode chamar. A devolução não depende do vencimento.
    /// @param idTrava Identificador da trava a liberar.
    function liberarTrava(uint256 idTrava) external {
        Trava storage trava = travas[idTrava];

        require(trava.ativa, "Trava inativa");
        require(msg.sender == trava.credor, "Apenas o credor");

        trava.ativa = false;
        travasAtivasPorParticipante[trava.holder] -= 1;
        travasAtivasPorParticipante[trava.credor] -= 1;

        _transferirTokensDaTrava(address(this), trava.holder, trava.amountSc);
        emit TravaLiberada(idTrava);
    }

    /// @notice Executa uma trava vencida e transfere os SJC bloqueados ao credor.
    /// @dev Só o credor da trava pode chamar, e apenas a partir do vencimento registrado.
    /// @param idTrava Identificador da trava a executar.
    function executarTrava(uint256 idTrava) external {
        Trava storage trava = travas[idTrava];

        require(trava.ativa, "Trava inativa");
        require(msg.sender == trava.credor, "Apenas o credor");
        require(block.timestamp >= trava.vencimento, "Ainda nao venceu");

        trava.ativa = false;
        travasAtivasPorParticipante[trava.holder] -= 1;
        travasAtivasPorParticipante[trava.credor] -= 1;

        _transferirTokensDaTrava(address(this), trava.credor, trava.amountSc);
        emit TravaExecutada(idTrava);
    }
}
