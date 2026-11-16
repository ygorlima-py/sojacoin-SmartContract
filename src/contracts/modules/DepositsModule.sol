// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {GuaranteesModule} from "./GuaranteesModule.sol";

// Depósitos, romaneios e emissão de SJC. Depósitos usam a reserva de CPRs
// definida em GuaranteesModule antes de emitir a parte livre ao produtor.
abstract contract DepositsModule is GuaranteesModule {
    /// @notice Calcula o digest EIP-712 da autorização do armazém para um depósito.
    /// @dev Usa o nonce atual do produtor informado. A consulta não registra nem altera o depósito.
    /// @param produtor Carteira do produtor que fará a chamada de registro.
    /// @param idDeposit Identificador que será usado para este depósito.
    /// @param chaveNFhash Hash da nota fiscal vinculada ao depósito.
    /// @param deadline Data Unix até a qual a autorização poderá ser utilizada.
    /// @return digest Mensagem que o armazém precisa assinar.
    function hashAutorizacaoDeposito(
        address produtor,
        uint256 idDeposit,
        bytes32 chaveNFhash,
        uint256 deadline
    ) external view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                AUTORIZACAO_DEPOSITO_TYPEHASH,
                produtor,
                idDeposit,
                chaveNFhash,
                noncesDeposito[produtor],
                deadline
            )
        );
        return _hashTypedDataV4(structHash);
    }

    /// @notice Registra um pedido de depósito autorizado digitalmente pelo armazém.
    /// @dev O próprio produtor cadastrado envia a transação; a assinatura do armazém
    /// confirma que ele autorizou esse produtor, ID, hash da nota e prazo.
    /// O registro aumenta a contagem de depósitos pendentes do produtor.
    /// @param idDeposit Identificador único do depósito.
    /// @param chaveNFhash Hash da nota fiscal associada à carga.
    /// @param deadline Data Unix de expiração da autorização assinada.
    /// @param assinaturaArmazem Assinatura EIP-712 do armazém para estes dados.
    function registrarDeposito(
        uint256 idDeposit,
        bytes32 chaveNFhash,
        uint256 deadline,
        bytes calldata assinaturaArmazem
    ) external {
        require(
            participanteCadastrado[msg.sender],
            "voce nao possui autorizacao para realizar essa operacao"
        );
        require(idDeposit > 0, "ID invalido");
        require(chaveNFhash != bytes32(0), "Hash da NF invalida");
        require(block.timestamp <= deadline, "Autorizacao expirada");
        require(
            deposits[idDeposit].depositante == address(0),
            "Id do deposito ja utilizado"
        );

        uint256 nonce = noncesDeposito[msg.sender];
        bytes32 structHash = keccak256(
            abi.encode(
                AUTORIZACAO_DEPOSITO_TYPEHASH,
                msg.sender,
                idDeposit,
                chaveNFhash,
                nonce,
                deadline
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        require(
            SignatureChecker.isValidSignatureNow(
                wareHouse,
                digest,
                assinaturaArmazem
            ),
            "Autorizacao do armazem invalida"
        );

        noncesDeposito[msg.sender] = nonce + 1;

        deposits[idDeposit].depositante = msg.sender;
        deposits[idDeposit].chaveNFhash = chaveNFhash;
        deposits[idDeposit].depositDate = block.timestamp;
        quantidadeDepositosPorNF[chaveNFhash] += 1;
        depositosPendentes[msg.sender] += 1;

        emit DepositoRegistrado(
            idDeposit,
            msg.sender,
            chaveNFhash,
            block.timestamp
        );
    }

    /// @notice Cancela um depósito que ainda não teve SJC emitido.
    /// @dev O produtor pode cancelar antes da confirmação do recebimento; o armazém
    /// pode cancelar antes da emissão, inclusive após confirmar. Se a entrada já foi
    /// contabilizada depois da última auditoria, ela é removida do estoque registrado.
    /// @param idDeposit Identificador do depósito a cancelar.
    function cancelarDeposito(uint256 idDeposit) external {
        Deposit storage deposit = deposits[idDeposit];
        require(deposit.depositante != address(0), "Deposito inexistente");
        require(!deposit.tokensIssued, "Tokens ja emitidos");

        bool chamadoPeloArmazem = msg.sender == wareHouse;
        require(
            chamadoPeloArmazem || msg.sender == deposit.depositante,
            "Nao autorizado"
        );
        require(
            chamadoPeloArmazem || !deposit.recebimentoConfirmado,
            "Somente o armazem pode cancelar apos confirmacao"
        );

        address depositante = deposit.depositante;
        bytes32 chaveNFhash = deposit.chaveNFhash;
        bytes32 hashRomaneio = deposit.hashRomaneio;

        if (deposit.recebimentoConfirmado) {
            // Se a confirmação aconteceu depois da última auditoria, desfaz
            // a entrada contabilizada. Uma auditoria posterior já substituiu
            // esse movimento pelo estoque contado fisicamente.
            if (deposit.dataTimestampDocumento > ultimaAuditoria) {
                uint256 massaDepositadaMg = deposit.pesoLiquidoKg * MG_POR_KG;
                require(
                    estoqueRegistradoMg >= massaDepositadaMg,
                    "Estoque registrado insuficiente"
                );
                estoqueRegistradoMg -= massaDepositadaMg;
                romaneioUtilizado[hashRomaneio] = false;
                _atualizarTetoEmissao();
                _pausarSeSemLastro(msg.sender);
            }
            // Quando a auditoria já incluiu a carga, o romaneio cancelado
            // permanece marcado como usado para impedir crédito duplicado.
        }

        quantidadeDepositosPorNF[chaveNFhash] -= 1;
        depositosPendentes[depositante] -= 1;
        delete deposits[idDeposit];

        emit DepositoCancelado(idDeposit, depositante, chaveNFhash, hashRomaneio);
    }

    /// @notice Confirma o recebimento da carga e registra o peso líquido do romaneio.
    /// @dev Só o armazém pode chamar. Calcula o peso líquido subtraindo tara e descontos
    /// em kg; se a data do romaneio for posterior à última auditoria, soma a entrada ao estoque.
    /// @param idDeposit Identificador do depósito previamente registrado.
    /// @param romaneio Pesos, descontos, hashes dos documentos e data do recebimento.
    function confirmarRecebimento(
        uint256 idDeposit,
        Romaneio calldata romaneio
    ) external {
        require(msg.sender == wareHouse, "Nao autorizado");

        Deposit storage deposit = deposits[idDeposit];
        require(deposit.depositante != address(0), "Deposito Inexistente");
        require(!deposit.recebimentoConfirmado, "Deposito ja confirmado");
        require(romaneio.hashRomaneio != bytes32(0), "Hash do romaneio invalida");
        require(!romaneioUtilizado[romaneio.hashRomaneio], "Romaneio ja utilizado");
        require(romaneio.dataTimestamp != 0, "Data do romaneio invalida");
        require(romaneio.dataTimestamp <= block.timestamp, "Data futura invalida");

        require(
            romaneio.pesoBrutoKg > romaneio.taraKg,
            "Tara maior que o bruto"
        );
        uint256 pesoCargaKg = romaneio.pesoBrutoKg - romaneio.taraKg;

        uint256 totalDescontos =
            romaneio.descontoUmidadeKg +
            romaneio.descontoImpurezasKg +
            romaneio.descontoAvariadosKg +
            romaneio.outrosDescontosKg;
        require(
            pesoCargaKg > totalDescontos,
            "Descontos maiores que o bruto"
        );

        uint256 pesoLiquidoKg = pesoCargaKg - totalDescontos;
        deposit.pesoLiquidoKg = pesoLiquidoKg;
        deposit.hashRomaneio = romaneio.hashRomaneio;
        deposit.dataTimestampDocumento = romaneio.dataTimestamp;
        deposit.recebimentoConfirmado = true;
        romaneioUtilizado[romaneio.hashRomaneio] = true;

        // Se a data do recebimento for posterior à última auditoria, soma a
        // entrada ao estoque contado. Uma auditoria posterior já a inclui.
        if (romaneio.dataTimestamp > ultimaAuditoria) {
            estoqueRegistradoMg += pesoLiquidoKg * MG_POR_KG;
            _atualizarTetoEmissao();
        }

        emit RecebimentoConfirmado(
            idDeposit,
            msg.sender,
            deposit.depositante,
            romaneio.pesoBrutoKg,
            romaneio.taraKg,
            pesoLiquidoKg,
            romaneio.hashRomaneio,
            romaneio.hashLaudo,
            romaneio.dataTimestamp,
            block.timestamp
        );

        emit ClassificacaoRecebimento(
            idDeposit,
            romaneio.umidadeBps,
            romaneio.impurezasBps,
            romaneio.avariadosBps,
            romaneio.descontoUmidadeKg,
            romaneio.descontoImpurezasKg,
            romaneio.descontoAvariadosKg,
            romaneio.outrosDescontosKg
        );
    }

    /// @notice Emite SJC com base no peso líquido de um depósito confirmado.
    /// @dev Só o armazém pode chamar, com auditoria dentro do prazo e teto de lastro suficiente.
    /// Reserva primeiro os SJC exigidos pelas CPRs do produtor, em ordem; emite a sobra
    /// livre ao produtor e carrega o resto inferior à menor unidade para o próximo depósito.
    /// @param idDeposit Identificador do depósito cujo recebimento foi confirmado.
    function emitirTokens(uint256 idDeposit) external {
        Deposit storage deposit = deposits[idDeposit];

        require(msg.sender == wareHouse, "Operacao nao autorizada");
        require(deposit.recebimentoConfirmado, "Falta aprovacao do armazem");
        require(!deposit.tokensIssued, "Tokens ja emitidos");
        require(
            block.timestamp <= ultimaAuditoria + PRAZO_AUDITORIA,
            "Auditoria vencida"
        );

        uint256 massaComRestoMg =
            deposit.pesoLiquidoKg * MG_POR_KG +
            restoConversaoMgPorProdutor[deposit.depositante];
        uint256 amountSc = massaComRestoMg / MG_POR_UNIDADE_SJC;
        uint8 novoRestoMg = uint8(massaComRestoMg % MG_POR_UNIDADE_SJC);
        require(amountSc > 0, "Quantidade abaixo da menor unidade SJC");

        require(totalSupply() + amountSc <= tetoEmissaoSc, "Acima do lastro");

        restoConversaoMgPorProdutor[deposit.depositante] = novoRestoMg;
        deposit.amountSc = amountSc;

        // Reserva primeiro a parte comprometida por CPRs ativas. Somente o
        // excedente é emitido livremente para o produtor.
        uint256 amountLivreSc = _reservarParaCPRs(
            deposit.depositante,
            idDeposit,
            amountSc
        );
        if (amountLivreSc > 0) {
            _mint(deposit.depositante, amountLivreSc);
        }

        deposit.tokensIssued = true;
        depositosPendentes[deposit.depositante] -= 1;

        emit EmissaoTokens(
            idDeposit,
            deposit.depositante,
            amountSc,
            novoRestoMg,
            block.timestamp
        );
    }
}
