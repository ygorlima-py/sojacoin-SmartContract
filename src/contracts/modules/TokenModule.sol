// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {ERC20Pausable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {SojaCoinStorage} from "../storage/SojaCoinStorage.sol";

// Regras da movimentação do SJC. Os depósitos, garantias e resgates usam
// as rotinas internas da base para movimentar tokens mantidos pelo contrato.
abstract contract TokenModule is
    SojaCoinStorage,
    ERC20,
    ERC20Burnable,
    ERC20Pausable,
    Ownable2Step,
    ERC20Permit
{
    /// @notice Inicializa o token, o dono inicial e a assinatura EIP-2612 do SJC.
    /// @param initialOwner Carteira que receberá a propriedade inicial do contrato.
    constructor(address initialOwner)
        ERC20("SojaCoin", "SJC")
        Ownable(initialOwner)
        ERC20Permit("SojaCoin")
    {}

    /// @notice Aplica as regras comuns a transferências, emissões e queimas de SJC.
    /// @dev Destinatários de transferências normais precisam estar cadastrados. A entrada
    /// ou saída de SJC do próprio contrato exige uma operação interna autorizada. Durante
    /// a liquidação controlada de um resgate, a rotina usa a implementação ERC-20 sem a pausa.
    /// @param from Carteira de origem; zero indica emissão de SJC.
    /// @param to Carteira de destino; zero indica queima de SJC.
    /// @param value Quantidade movimentada em unidades internas do token.
    function _update(
        address from,
        address to,
        uint256 value
    ) internal virtual override(ERC20, ERC20Pausable) {
        if (to != address(0)) {
            if (to == address(this) || from == address(this)) {
                require(
                    _movimentacaoDeTrava,
                    "Movimentacao de trava nao autorizada"
                );
            } else {
                require(
                    participanteCadastrado[to],
                    "destino nao cadastrado"
                );
            }
        }

        if (_liquidacaoResgate) {
            require(from == address(this), "Liquidacao de resgate invalida");
            ERC20._update(from, to, value);
        } else {
            super._update(from, to, value);
        }
    }

    /// @notice Informa que o SJC usa seis casas decimais.
    /// @return Quantidade de casas decimais exibidas para o token.
    function decimals() public pure virtual override returns (uint8) {
        return 6;
    }

    /// @notice Desativa a renúncia à propriedade do contrato.
    /// @dev O dono pode transferir a propriedade, mas esta chamada sempre reverte
    /// para manter um responsável administrativo configurado.
    function renounceOwnership() public view virtual override onlyOwner {
        revert("Renuncia de dono desativada");
    }

    /// @notice Transfere SJC em uma operação interna de trava, CPR ou resgate.
    /// @dev Ativa temporariamente a permissão para o contrato receber ou devolver tokens;
    /// a operação ERC-20 ainda respeita a pausa global.
    /// @param from Carteira de onde os tokens sairão.
    /// @param to Carteira que receberá os tokens.
    /// @param amountSc Quantidade transferida em unidades internas do SJC.
    function _transferirTokensDaTrava(
        address from,
        address to,
        uint256 amountSc
    ) internal {
        _movimentacaoDeTrava = true;
        _transfer(from, to, amountSc);
        _movimentacaoDeTrava = false;
    }

    /// @notice Devolve ao titular os SJC que estavam presos em um pedido de resgate.
    /// @dev Ativa uma exceção temporária à pausa global somente para transferir do contrato
    /// ao titular. A função não permite movimentar tokens entre participantes.
    /// @param to Carteira do titular que receberá os tokens devolvidos.
    /// @param amountSc Quantidade devolvida em unidades internas do SJC.
    function _transferirTokensDoResgate(address to, uint256 amountSc) internal {
        _movimentacaoDeTrava = true;
        _liquidacaoResgate = true;
        _transfer(address(this), to, amountSc);
        _liquidacaoResgate = false;
        _movimentacaoDeTrava = false;
    }

    /// @notice Queima os SJC mantidos pelo contrato após a conclusão de um resgate.
    /// @dev A exceção temporária à pausa só permite queima a partir do saldo do próprio contrato.
    /// @param amountSc Quantidade que será queimada, em unidades internas do SJC.
    function _queimarTokensDoResgate(uint256 amountSc) internal {
        _liquidacaoResgate = true;
        _burn(address(this), amountSc);
        _liquidacaoResgate = false;
    }

    /// @notice Recalcula o teto de emissão usando o estoque registrado em miligramas.
    /// @dev Um incremento mínimo de SJC representa 60 mg; o resultado é arredondado para baixo.
    function _atualizarTetoEmissao() internal {
        tetoEmissaoSc = estoqueRegistradoMg / MG_POR_UNIDADE_SJC;
    }

    /// @notice Pausa o contrato e registra o horário da pausa, se ainda não estiver pausado.
    /// @dev Função interna usada pela administração e pelos controles de lastro.
    function _pausarContrato() internal {
        if (!paused()) {
            ultimaPausa = block.timestamp;
            _pause();
        }
    }

    /// @notice Emite um alerta e pausa o contrato se os SJC emitidos excederem o estoque registrado.
    /// @dev Compara o total de SJC convertido em miligramas com o estoque contabilizado.
    /// @param detectadaPor Carteira ou operação que identificou a divergência.
    function _pausarSeSemLastro(address detectadaPor) internal {
        if (totalSupply() * MG_POR_UNIDADE_SJC > estoqueRegistradoMg) {
            emit DivergenciaDetectada(
                detectadaPor,
                wareHouse,
                estoqueRegistradoMg,
                totalSupply(),
                block.timestamp
            );
            _pausarContrato();
        }
    }
}
