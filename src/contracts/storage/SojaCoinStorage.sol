// SPDX-License-Identifier: MIT
// Compatible with OpenZeppelin Contracts ^5.7.0
pragma solidity ^0.8.27;

// Base comum dos módulos. Guarda os dados e as rotinas internas compartilhadas
// pelo contrato; não expõe operações de negócio por conta própria.
abstract contract SojaCoinStorage {
    // 1 SJC representa uma saca de 60 kg. O token usa seis casas decimais.
    // 1 SJC vale 60 kg. Com seis casas, uma unidade mínima equivale a 60 mg.
    // Quantidades passadas às funções ERC20 são essas unidades internas.
    uint256 public constant UNIDADES_POR_SACA = 1_000_000;
    uint256 internal constant MG_POR_KG = 1_000_000;
    uint256 internal constant MG_POR_UNIDADE_SJC = 60;

    // Variáveis do depósito.
    struct Deposit {
        address depositante;
        bytes32 chaveNFhash;
        // Valores de SJC em unidades internas (1 SJC = 1.000.000 unidades).
        uint256 amountSc;
        bool recebimentoConfirmado;
        bool tokensIssued;
        uint256 depositDate;
        uint256 pesoLiquidoKg;
        bytes32 hashRomaneio;
        uint256 dataTimestampDocumento;
    }

    struct Romaneio {
        // Pesagem.
        uint256 pesoBrutoKg;
        uint256 taraKg;

        // Classificação em centésimos de percentual.
        uint16 umidadeBps;
        uint16 impurezasBps;
        uint16 avariadosBps;

        // Descontos efetivamente aplicados.
        uint256 descontoUmidadeKg;
        uint256 descontoImpurezasKg;
        uint256 descontoAvariadosKg;
        uint256 outrosDescontosKg;

        // Identificação.
        bytes32 hashRomaneio;
        bytes32 hashLaudo;
        // Data do recebimento informada pelo armazém, em segundos Unix.
        uint256 dataTimestamp;
    }

    // Tipos de serviços que podem ser cobrados separadamente do romaneio.
    // "Outro" usa o documento vinculado para detalhar o serviço.
    enum TipoServico {
        Recepcao,
        Limpeza,
        Secagem,
        Armazenagem,
        Expedicao,
        Transbordo,
        Pesagem,
        EmissaoDocumento,
        Outro
    }

    // A cobrança pode ser somente informativa em reais, ou paga em SJC.
    // Valores em reais usam centavos para não usar números decimais no contrato.
    enum UnidadeTarifa {
        ReaisCentavos,
        SJC
    }

    enum StatusServicoETarifa {
        Inexistente,
        Aberto,
        PagoEmSJC,
        PagoEmReais,
        Cancelado
    }

    // Uma linha de serviço é ligada ao depósito, mas não altera os quilos
    // aceitos no romaneio nem a quantidade de SJC emitida para ele.
    struct ServicoETarifa {
        uint256 idDeposit;
        address produtor;
        address recebedor;
        TipoServico tipo;
        UnidadeTarifa unidade;
        uint256 valor;
        bytes32 hashCobranca;
        bytes32 hashComprovantePagamento;
        StatusServicoETarifa status;
    }

    // Garantia formada com SJC que o titular já possui.
    struct Trava {
        address holder;
        address credor;
        uint256 amountSc;
        bytes32 hashAcordo;
        uint256 vencimento;
        bool ativa;
    }

    // Estados de uma CPR usada para reservar automaticamente SJC futuros.
    enum StatusCPRGarantia {
        Inexistente,
        Ativa,
        Coberta,
        Executada,
        Liberada,
        Cancelada
    }

    // A CPR continua existindo como documento fora da blockchain. Esta estrutura
    // guarda apenas a regra de reserva de SJC aceita pelo produtor.
    struct CPRGarantia {
        address produtor;
        address credor;
        uint256 valorGarantidoSc;
        uint256 bloqueadoSc;
        uint256 ordemExecucao;
        uint256 vencimento;
        bytes32 hashCPR;
        StatusCPRGarantia status;
    }

    // Estados possíveis de um resgate.
    // Inexistente fica primeiro para IDs sem cadastro não parecerem válidos.
    enum StatusResgate {
        Inexistente,
        Solicitado,
        Autorizado,
        Concluido,
        Cancelado
    }

    // Dados guardados para cada pedido de retirada.
    struct Resgate {
        address holder;
        uint256 amountSc;
        StatusResgate status;
        uint256 autorizadoEm;
    }

    // Guarda a última pausa para exigir auditoria posterior antes do desbloqueio.
    uint256 public ultimaPausa;

    // Impede dois endereços de se cadastrarem novamente.
    mapping(address => bool) public participanteCadastrado;

    // Carteira operacional do armazém e troca pendente em duas etapas.
    address public wareHouse;
    address public pendingWareHouse;

    // Chave do auditor responsável pela auditoria.
    address public auditor;

    // Data da última auditoria.
    uint256 public ultimaAuditoria;

    // Limite total de SJC lastreado, medido em unidades internas do token.
    uint256 public tetoEmissaoSc;

    // Estoque físico contabilizado em miligramas. Auditorias substituem o saldo;
    // recebimentos confirmados somam e retiradas concluídas subtraem.
    uint256 public estoqueRegistradoMg;

    // Restante da conversão kg -> unidades fracionárias de SJC, por produtor.
    // O valor fica entre 0 e 59 mg e é carregado no próximo depósito emitido.
    mapping(address => uint8) public restoConversaoMgPorProdutor;

    // Prazo máximo entre auditorias.
    uint256 public constant PRAZO_AUDITORIA = 60 days;

    // Limita a fila ativa para manter a emissão executável e previsível.
    uint256 public constant MAX_CPRS_ATIVAS_POR_PRODUTOR = 16;

    // Após o vencimento, o dono pode arbitrar uma CPR não resolvida.
    uint256 public constant CARENCIA_CPR = 30 days;

    // Gera o próximo ID de trava.
    uint256 public proximaTrava;

    // Gera o próximo ID de serviço ou tarifa registrada.
    uint256 public proximoServicoETarifa;

    // Fica true somente durante uma transferência feita pelas funções de trava.
    bool internal _movimentacaoDeTrava;

    // Autoriza somente liquidações de resgate a ignorar a pausa global.
    bool internal _liquidacaoResgate;

    // Um romaneio identifica um recebimento físico e só pode ser usado uma vez.
    mapping(bytes32 => bool) public romaneioUtilizado;

    // Uma NF pode estar vinculada a vários romaneios; a contagem ajuda a consultar isso.
    mapping(bytes32 => uint256) public quantidadeDepositosPorNF;

    // Mapeia cada ID para os dados de uma garantia em SJC já emitido.
    mapping(uint256 => Trava) public travas;

    // Serviços e tarifas não alteram a classificação nem a emissão do depósito.
    mapping(uint256 => ServicoETarifa) public servicosETarifas;
    mapping(uint256 => uint256[]) internal _servicosETarifasDoDeposito;

    // Mapeamento de depósitos por ID.
    mapping(uint256 => Deposit) public deposits;

    // Conta os depósitos ainda não emitidos ou cancelados de cada participante.
    mapping(address => uint256) public depositosPendentes;

    // Evita remover alguém que ainda tenha uma retirada ou garantia em andamento.
    mapping(address => uint256) public resgatesPendentesPorHolder;
    mapping(address => uint256) public travasAtivasPorParticipante;
    mapping(address => uint256) public cprsAtivasPorParticipante;

    // Nonce individual para impedir o reuso de autorizações de depósito.
    mapping(address => uint256) public noncesDeposito;

    bytes32 public constant AUTORIZACAO_DEPOSITO_TYPEHASH = keccak256(
        "AutorizacaoDeposito(address produtor,uint256 idDeposit,bytes32 chaveNFhash,uint256 nonce,uint256 deadline)"
    );

    // O produtor autoriza a inclusão da sua CPR uma única vez, por assinatura.
    mapping(address => uint256) public noncesCPR;
    bytes32 public constant AUTORIZACAO_CPR_TYPEHASH = keccak256(
        "AutorizacaoCPRGarantia(address produtor,address credor,uint256 valorGarantidoSc,uint256 ordemExecucao,uint256 vencimento,bytes32 hashCPR,uint256 nonce,uint256 deadline)"
    );

    mapping(uint256 => CPRGarantia) public cprsGarantia;
    mapping(address => uint256[]) internal _cprsAtivasDoProdutor;
    mapping(address => uint256) public ultimaOrdemCPRPorProdutor;
    uint256 public proximaCPRGarantia;

    // Guarda cada resgate pelo ID.
    mapping(uint256 => Resgate) public resgates;

    // Gera o próximo ID de resgate.
    uint256 public proximoResgate;

    // Conta as unidades SJC autorizadas que ainda não foram concluídas ou canceladas.
    uint256 public sacasEmRetirada;

    // Prazo para o dono arbitrar um resgate autorizado sem resolução.
    uint256 public constant PRAZO_RETIRADA = 7 days;

    // Evento para registrar que os tokens foram bloqueados.
    event TokensTravados(
        uint256 indexed idTrava,
        address indexed titular,
        address indexed credor,
        uint256 amountSc,
        bytes32 hashAcordo
    );

    event TravaLiberada(uint256 indexed idTrava);
    event TravaExecutada(uint256 indexed idTrava);

    event ServicoETarifaRegistrado(
        uint256 indexed idServicoETarifa,
        uint256 indexed idDeposit,
        address indexed produtor,
        address recebedor,
        TipoServico tipo,
        UnidadeTarifa unidade,
        uint256 valor,
        bytes32 hashCobranca
    );

    event ServicoPagoEmSJC(
        uint256 indexed idServicoETarifa,
        address indexed produtor,
        address indexed recebedor,
        uint256 valorSc
    );

    event ServicoPagoEmReais(
        uint256 indexed idServicoETarifa,
        bytes32 hashComprovantePagamento
    );

    event ServicoETarifaCancelado(uint256 indexed idServicoETarifa);

    event AuditorCadastrado(
        address indexed walletAddressAuditor,
        address indexed cadastradoPor,
        uint256 dataTimestamp
    );

    event AuditoriaRegistrada(
        address indexed walletAddressAuditor,
        address indexed walletAddressArmazenador,
        uint256 estoqueFisicoKg,
        uint256 totalTokensUnidadesSc,
        bytes32 hashLaudo,
        uint256 dataTimestamp
    );

    event DivergenciaDetectada(
        address indexed detectadaPor,
        address indexed walletAddressArmazenador,
        uint256 estoqueRegistradoMg,
        uint256 totalTokensUnidadesSc,
        uint256 dataTimestamp
    );

    event ParticipanteCadastrado(
        address indexed walletAddress,
        bytes32 hashCadastro,
        address indexed cadastradoPor
    );

    event ParticipanteRemovido(address indexed walletAddress);

    event DepositoRegistrado(
        uint256 indexed idDeposit,
        address indexed depositante,
        bytes32 indexed chaveNFhash,
        uint256 dataTimestamp
    );

    event DepositoCancelado(
        uint256 indexed idDeposit,
        address indexed depositante,
        bytes32 chaveNFhash,
        bytes32 hashRomaneio
    );

    event RecebimentoConfirmado(
        uint256 indexed idDeposit,
        address indexed warehouse,
        address indexed depositante,
        uint256 pesoBrutoKg,
        uint256 taraKg,
        uint256 pesoLiquidoKg,
        bytes32 hashRomaneio,
        bytes32 hashLaudo,
        uint256 dataTimestampDocumento,
        uint256 dataTimestamp
    );

    event ClassificacaoRecebimento(
        uint256 indexed idDeposit,
        uint16 umidadeBps,
        uint16 impurezasBps,
        uint16 avariadosBps,
        uint256 descontoUmidadeKg,
        uint256 descontoImpurezasKg,
        uint256 descontoAvariadosKg,
        uint256 outrosDescontosKg
    );

    event EmissaoTokens(
        uint256 indexed idDeposit,
        address indexed wallet,
        uint256 amount,
        uint8 restoConversaoMg,
        uint256 dataTimestamp
    );

    event CPRGarantiaRegistrada(
        uint256 indexed idCPR,
        address indexed produtor,
        address indexed credor,
        uint256 valorGarantidoSc,
        uint256 ordemExecucao,
        bytes32 hashCPR,
        uint256 vencimento
    );

    event CPRGarantiaBloqueada(
        uint256 indexed idCPR,
        uint256 indexed idDeposit,
        uint256 amountSc,
        uint256 totalBloqueadoSc
    );

    event CPRGarantiaExecutada(
        uint256 indexed idCPR,
        address indexed credor,
        uint256 amountSc
    );

    event CPRGarantiaLiberada(
        uint256 indexed idCPR,
        address indexed produtor,
        uint256 amountSc
    );

    event CPRGarantiaCancelada(uint256 indexed idCPR);

    event CPRGarantiaArbitrada(
        uint256 indexed idCPR,
        bool entregueAoCredor,
        uint256 amountSc
    );

    event NovoArmazemProposto(
        address indexed armazemAtual,
        address indexed novoArmazem,
        address indexed propostoPor
    );

    event ArmazemAlterado(
        address indexed armazemAnterior,
        address indexed novoArmazem
    );

    event ResgateSolicitado(
        uint256 indexed idResgate,
        address indexed holder,
        uint256 amountSc
    );

    event RetiradaAutorizada(uint256 indexed idResgate, uint256 amountSc);

    event RetiradaConcluida(
        uint256 indexed idResgate,
        bytes32 hashNFRetorno
    );

    event ResgateCancelado(uint256 indexed idResgate);

}
