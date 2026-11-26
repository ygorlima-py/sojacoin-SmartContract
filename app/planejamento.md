# Planejamento do MVP do SojaCoin

## Objetivo

Permitir que uma empresa registre participantes, registre CPRs, receba soja e emita SJC. Cada participante aceita os termos da empresa; o produtor assina a CPR e a aprovação do lote com sua carteira. Na emissão, o contrato abate as CPRs compatíveis e destina os tokens aos respectivos credores; o excedente vai para o produtor.

O MVP será uma aplicação Django com templates HTML e Tailwind, com JavaScript para conectar a MetaMask. Em interações que alteram apenas uma parte da tela, usar HTMX para atualizar um fragmento HTML sem recarregar a página inteira. As operações serão reais em rede de teste. Uma empresa terá sua própria instância dos contratos; várias unidades da mesma empresa poderão usá-la. A equipe da plataforma criará a empresa e as primeiras contas de forma assistida e ajudará na implantação, conexão dos contratos e configuração e aprovações da Safe. Não haverá cadastro público de contas nesta versão.

## Fluxo principal

1. A empresa é cadastrada no SaaS e os endereços dos contratos implantados são informados.
2. O administrador cadastra a unidade, o auditor e os funcionários, e configura as permissões exigidas no contrato.
3. O auditor registra a auditoria e o estoque inicial da unidade.
4. Participantes são cadastrados, recebem uma proposta e aceitam o termo com suas carteiras. Um participante pode representar um produtor ou outro papel; CPRs e operações específicas do produtor continuam em etapas posteriores.
5. A CPR é preenchida e assinada pelo produtor; sua transação é enviada à rede.
6. O funcionário abre o recebimento, informa pesagem, classificação e documentos e confirma a carga. A nota fiscal é vinculada à carga.
7. Um funcionário autorizado cria o lote; o produtor confere e assina a aprovação.
8. A aprovação é enviada ao contrato e a emissão é executada por uma carteira autorizada. O contrato distribui os tokens conforme as CPRs compatíveis.
9. As partes consultam o resultado e o histórico no sistema.

A aprovação do lote e a emissão são chamadas separadas nos contratos atuais. O SaaS pode encaminhá-las em sequência, mostrando aprovação e emissão pendentes até a confirmação de cada transação. A assinatura, sozinha, não confirma a emissão. O mecanismo da carteira que enviará as transações autorizadas será definido na integração; não presumir que o produtor possui essa permissão.

## Apps Django

| App | Responsabilidade | Situação |
| --- | --- | --- |
| `accounts` | Autenticação, perfil e carteiras dos usuários. O vínculo com a empresa fica em `companies`. | `UserProfile`, `Wallet`, `UserWallet` e `WalletVerificationChallenge` implementados com migrações, Admin e testes; login, logout, verificação criptográfica, carteiras na tela e Safe ainda pendentes. O usuário padrão do Django continua sendo usado. |
| `companies` | Empresas, unidades, equipe e permissões no SaaS. | Modelos implementados e migrados; telas e rotas de empresas, unidades, vínculos de equipe, funções e acessos por unidade implementadas. Operações blockchain ainda pendentes. |
| `participants` | Participantes da empresa, versões do termo, convite remoto e aceite na blockchain. | Cadastro e aceite de participantes implementados com isolamento por empresa, PDF de termos publicado por administrador da empresa, versões imutáveis, link temporário, assinatura EIP-712, transações e confirmação de eventos. O cadastro pode representar um produtor ou outro participante; a empresa é responsável por revisar e aprovar o conteúdo. |
| `documents` | Arquivos, hashes e vínculos com operações. | Planejado. |
| `audits` | Auditorias e estoque informado pelo auditor. | Planejado. |
| `cprs` | CPRs, assinaturas e abatimentos. | Planejado. |
| `receipts` | Recebimentos, pesagem, classificação, documentos e transações de entrada da carga. | Cadastro SaaS, PDFs privados, cálculo de peso líquido e fluxo on-chain `openLoadReceipt` → `confirmLoadReceipt` → `linkTaxInvoice` implementados. Cada tentativa é persistida; o servidor verifica carteira, rede, contratos, eventos e estado do depósito. Lotes e emissão continuam pendentes. |
| `issuance` | Lotes, aprovação do produtor e emissão. | Planejado. |
| `blockchain` | Redes, contratos, transações e sincronização dos eventos. | Persistência, RPC por rede, confirmação de recibos e processamento idempotente de eventos implementados; participantes usam esses serviços. |
| `dashboard` | Página inicial da empresa, métricas operacionais, pendências, atalhos e operações recentes por perfil. | Implementado como consulta somente leitura; administradores veem pendências e até cinco emissões confirmadas da empresa, sem ganhar permissão operacional; operadores veem operações nas unidades autorizadas. Não possui modelos próprios. |

Um app Django agrupa uma parte do código; ele não é uma aplicação separada para instalar no celular.

## Interações parciais com HTMX

O sistema continua sendo server-rendered: o Django gera as páginas e as views
continuam sendo baseadas em classe. HTMX será usado apenas onde a atualização
parcial melhorar a operação e não exigir uma aplicação SPA.

### Usos planejados

- atualizar tabelas, filtros e contadores sem recarregar a página inteira;
- abrir e fechar painéis, modais e formulários inline;
- validar ou salvar uma parte de um formulário e substituir somente seu cartão;
- atualizar o status de uma operação ou transação pendente enquanto o usuário
  permanece na tela;
- atualizar listas de notificações e pendências do painel.

### Regras de implementação

- Cada endpoint HTMX deve continuar funcionando com uma requisição HTTP normal
  quando isso fizer sentido; HTMX é uma melhoria de interação, não a única forma
  de executar uma operação.
- A view deve identificar `HX-Request` e retornar o fragmento correspondente
  quando solicitado. Uma requisição normal retorna a página completa ou redireciona
  como no fluxo tradicional do Django.
- Fragmentos ficam em uma pasta explícita do app, por exemplo
  `templates/receipts/partials/receipt_row.html`, para não misturar página
  completa com resposta parcial.
- Toda requisição HTMX que altera dados usa CSRF, verifica usuário, empresa,
  função, unidade e estado da operação exatamente como uma requisição completa.
- HTMX não assina transações e não recebe chaves privadas. Quando MetaMask for
  necessária, o JavaScript do navegador inicia a assinatura e o retorno é enviado
  ao Django pelo fluxo normal da operação.
- Não usar HTMX para esconder uma confirmação de assinatura, aprovação de Safe,
  emissão ou outra transação blockchain. Essas etapas devem mostrar claramente o
  que foi preparado, assinado, enviado e confirmado.
- Após uma transação, substituir somente o componente de status quando possível,
  sem apresentar como confirmado algo que ainda esteja pendente na rede.
- Testar cada interação em modo HTMX e em modo de carregamento completo, incluindo
  erros de validação, sessão expirada, permissão negada e falha de transação.

## Telas

São nove áreas principais. Listagem, detalhe e formulário podem compartilhar uma área. O quadro resume as ações principais e o estado de implementação do MVP.

| Tela | Ações principais | Quem usa |
| --- | --- | --- |
| Login | Entrar e sair. | Todos. |
| Página inicial | Ver pendências, últimas operações e atalhos conforme o perfil. | Todos. |
| Empresa, unidades e auditorias | Consultar empresa e contratos; cadastrar unidade, indicar auditor e registrar ou consultar auditoria. Configuração inicial assistida. | Administrador e auditor. |
| Equipe e permissões | Consultar usuários e indicar o que podem fazer e em quais unidades. A equipe da plataforma cria as contas iniciais de forma assistida. | Administrador. |
| Participantes | Cadastrar participantes, associar carteira e acompanhar aceite do termo. A categoria específica (como produtor ou credor) será definida nos fluxos operacionais correspondentes. | Administrador ou funcionário autorizado; participante para aceitar. |
| CPRs | Cadastrar, revisar, assinar e consultar saldo e abatimentos. | Revenda, produtor e credor conforme acesso. |
| Recebimentos | Abrir carga, registrar pesagem e classificação, anexar documentos e vincular nota fiscal. | Funcionário autorizado; produtor consulta suas cargas. |
| Lotes e aprovações | Criar lote, mostrar dados ao produtor, solicitar assinatura e acompanhar emissão. | Funcionário autorizado e produtor. |
| Saldos e histórico | Consultar saldo, destinação dos tokens e situação das transações. | Produtor, credor e empresa conforme acesso. |

O painel administrativo padrão do Django pode auxiliar na configuração inicial; as telas acima são para os usuários do produto.

### Estados e resultado que precisam aparecer

- Login sem vínculo ativo não concede acesso aos dados de uma empresa. Usuários participantes só acessam os registros aos quais foram vinculados.
- O cadastro de unidade no SaaS informa se ainda aguarda registro na rede; o auditor e as permissões só aparecem como configurados após confirmação da operação correspondente.
- Participantes exibem proposta, aceite pendente, confirmação, falha ou necessidade de conferência. CPRs exibem assinatura e registro pendentes, confirmados ou com falha, além do saldo confirmado.
- Recebimentos apresentam os dados da carga, a situação da nota e as transações pendentes, confirmadas ou com falha.
- Na emissão, distinguir assinatura recebida, aprovação pendente ou confirmada e emissão pendente, confirmada ou com erro. Não mostrar tokens como emitidos antes da confirmação.
- Em saldos e histórico, identificar a CPR, a carga, o credor que recebeu, o valor destinado ao produtor e a transação confirmada.

## Perfis e permissões do MVP

As funções abaixo são modelos iniciais de acesso, não cargos obrigatórios da empresa. Uma mesma pessoa pode receber mais de uma função quando não houver conflito com as regras do contrato. Cada vínculo vale para uma empresa e, nas operações de unidade, somente para as unidades autorizadas. Uma pessoa vinculada a duas empresas terá permissões independentes em cada uma.

| Perfil no SaaS | Pode visualizar | Pode iniciar ou executar no MVP |
| --- | --- | --- |
| Administrador da empresa | Dados da empresa, equipe, unidades, auditorias, participantes, CPRs, cargas, lotes e histórico da própria empresa. | Gerenciar acessos no SaaS; preparar configuração de unidade, auditor e carteira; preparar cadastro de participante e CPR. As ações restritas ao dono no contrato exigem a carteira proprietária apropriada. |
| Operador de cadastro | Participantes e CPRs da empresa para os quais recebeu acesso. | Preparar cadastro de participante e CPR, anexar documentos e solicitar assinatura. A proposta do participante no contrato exige o proprietário do SojaCoin. |
| Recebedor | Cargas e documentos necessários das unidades autorizadas e dados necessários dos produtores dessas cargas. | Abrir e confirmar recebimento e vincular nota fiscal, com carteira autorizada a receber soja naquela unidade. |
| Operador de emissão | Cargas elegíveis, lotes e resultado de emissão das unidades autorizadas. | Criar lote e encaminhar emissão com carteira autorizada a emitir naquela unidade. Não assina pelo produtor. |
| Auditor | Dados de estoque, documentos e histórico de auditoria das unidades para as quais foi designado. | Registrar auditoria com a carteira definida como auditora da unidade. |
| Produtor | Seus termos, suas CPRs, suas cargas, lotes, saldos e documentos relacionados. | Assinar seu termo, sua CPR e a aprovação dos próprios lotes; consultar andamento. |
| Credor | Termos, CPRs em que é credor, abatimentos, saldo e documentos vinculados a essas operações. | Aceitar seu termo e consultar o recebimento dos tokens. O cadastro da CPR atual não exige assinatura do credor no contrato. |

Para o MVP, o administrador pode delegar **preparação de cadastro** a um operador. A assinatura do produtor permanece necessária para registrar a CPR. O operador não recebe automaticamente o poder de proprietário na blockchain.

### Quem autoriza cada etapa

| Etapa | Regra do SaaS | Exigência do contrato atual |
| --- | --- | --- |
| Cadastrar unidade, definir auditor e permissões | Administrador da empresa prepara a operação. | Proprietário do `UnitRegistry` executa a chamada. |
| Propor cadastro do participante | Administrador ou operador de cadastro prepara. | Proprietário do SojaCoin executa a proposta. |
| Aceitar termo | Apenas o participante associado pode aprovar pelo SaaS. | Assinatura válida do participante; outra carteira pode enviar a transação. |
| Cadastrar CPR | Administrador ou operador de cadastro prepara; o produtor revisa e assina. | Assinatura válida do produtor, com produtor e credor cadastrados. Qualquer carteira pode enviar `registerCPR` com assinatura válida; a restrição de quem inicia pelo SaaS é uma regra adicional da aplicação. |
| Receber carga e vincular nota | Recebedor autorizado naquela unidade. | Carteira com permissão de recebimento na unidade. |
| Registrar auditoria | Auditor designado naquela unidade. | A chamada deve vir da carteira registrada como auditora da unidade. |
| Criar lote | Operador de emissão das unidades envolvidas. | A carteira que cria precisa de permissão de emissão em cada unidade das cargas incluídas. |
| Aprovar lote | Somente o produtor do lote assina. | Assinatura válida do produtor; outra carteira pode enviar a aprovação. |
| Emitir tokens | Operador de emissão autorizado ou mecanismo automático aprovado para essa operação. | Carteira que chama a emissão precisa da permissão de emissão na unidade da carga; lote e auditoria devem atender às condições do contrato. |

O dono do SojaCoin e o dono do `UnitRegistry` são verificações de propriedade separadas; a configuração deve confirmar quem é proprietário de cada contrato. Um administrador do SaaS não se torna proprietário na rede apenas por receber esse perfil. Em uma Safe, as aprovações necessárias seguem a configuração da Safe.

### Separação de funções e limites

- No contrato, uma carteira não pode acumular recebimento e emissão na mesma unidade, nem autorização e conclusão de retirada na mesma unidade. Uma carteira auditora não pode receber permissões que afetem o estoque na unidade auditada; essas permissões gerais também são bloqueadas enquanto ela for auditora de qualquer unidade, ativa ou inativa.
- O SaaS deve apresentar os conflitos antes de solicitar a transação e respeitar essas regras também quando a permissão geral se combinar com a específica da unidade. O contrato faz a verificação final.
- Definir permissões no contrato substitui a máscara anterior daquele escopo. Ao alterar uma permissão, a aplicação deve ler o conjunto existente e enviar o conjunto completo pretendido, preservando as demais permissões autorizadas.
- O vínculo entre usuário e carteira deve ser comprovado. Antes de uma operação, verificar usuário ativo, empresa e unidade corretas, ação permitida, carteira vinculada e permissão confirmada na rede. Alterações no SaaS não passam a valer na blockchain até a confirmação da transação correspondente.
- Um usuário sem acesso não deve ver nem acessar dados por URL, pesquisa, download, API ou alteração de identificadores. O produtor e o credor veem apenas registros relacionados a eles, inclusive em seus documentos e histórico. Dados públicos da blockchain podem ser consultados diretamente fora do SaaS.
- Registrar no histórico quem concedeu, alterou ou revogou acesso, a empresa, unidade, função, data e resultado da transação associada. A desativação do acesso no SaaS é imediata na aplicação; a revogação da carteira no contrato exige transação própria.
- O superusuário Django serve à administração técnica da plataforma e não é automaticamente o dono dos contratos nem um usuário de negócio de todas as empresas. O acesso operacional entre empresas deve ser concedido e registrado explicitamente.

## Regras gerais da modelagem

- Todos os modelos persistidos terão `created_at` e `updated_at`, com data e hora em UTC. Na interface, apresentar horário de Brasília quando adequado. `updated_at` registra a última alteração feita pelo Django; operações feitas fora do Django precisam atualizar esse campo explicitamente.
- Usar identificadores internos como chave primária e guardar separadamente os IDs gerados pelos contratos. Um ID da blockchain é único apenas dentro do contrato e da rede correspondentes.
- Todo dado empresarial deve estar vinculado à empresa diretamente ou por uma relação obrigatória. As consultas e alterações devem verificar a empresa e as permissões do usuário em todas as telas e operações.
- Nomes de campos abaixo são propostas para implementação. Chaves estrangeiras usam `PROTECT` quando apagar um registro destruiria o histórico; dados operacionais confirmados devem ser preservados por estados ou desativação.
- Guardar valores inteiros nas unidades do contrato: peso em kg ou mg conforme o campo; quantidade de tokens em unidades mínimas. Um SJC equivale a uma saca de 60 kg e a 1.000.000 de unidades mínimas. A interface pode mostrar sacas e kg, com conversão exata e sem números de ponto flutuante.
- Documentos ficam em armazenamento próprio com backup; o hash gravado em blockchain deve ser calculado sobre os mesmos bytes que serão guardados. A nota fiscal exige atenção: o campo do contrato representa o hash da chave da nota, e não necessariamente o hash do arquivo inteiro.
- Dados locais de saldo e estado são cópias para consulta. A confirmação da transação e os eventos da rede determinam o que foi efetivamente registrado no contrato. Uma operação pendente ou com erro não deve aparecer como concluída.

## Modelos de banco de dados propostos

Além dos campos abaixo, **cada modelo possui `id`, `created_at` e `updated_at`**. Para usuários, acrescentar esses horários por um perfil relacionado ao usuário padrão do Django nesta fase; o usuário padrão já está em uso pelo superusuário local. Decidir um eventual modelo de usuário próprio antes de avançar para produção, considerando a migração necessária.

### `accounts`

- **UserProfile**: `user` (um para um com o usuário Django), `full_name`, `phone`, `active`.
- **Wallet**: `address`, `address_normalized`, `label`, `wallet_type` (individual ou contrato, incluindo Safe), `active`. O endereço é único em formato normalizado. Nenhuma chave privada é armazenada.
- **UserWallet**: `user`, `wallet`, `verification_status`, `verified_at`. A associação deve ser comprovada por assinatura de um desafio, com prazo e uso único; uma carteira corporativa pode ter mais de um usuário vinculado.
- **WalletVerificationChallenge**: `user_wallet`, `message`, `digest`, `nonce`, `purpose`, `domain`, `payload_version`, `chain_id`, `expires_at`, `status`, `signature`, `consumed_at`, `cancelled_at`, `cancellation_reason`. O desafio é único, preso à rede indicada e mantém o histórico do conteúdo assinado; o model não verifica a assinatura sozinho.

### `companies`

- **Company**: `legal_name`, `trade_name`, `tax_id` (CNPJ), `active`. CNPJ normalizado e único por empresa cadastrada.
- **CompanyMembership**: `company`, `user`, `active`. Vínculo com a empresa; uma pessoa pode participar de mais de uma empresa. Restrição única para `company` + `user`.
- **MembershipRole**: `membership`, `role` (administrador, cadastro, recebimento, emissão ou auditoria), `active`, `granted_by`, `revoked_at`. Permite mais de uma função por vínculo; restringir duplicidade de função ativa. Produtor e credor são associados ao participante correspondente, não automaticamente à equipe interna.
- **Unit**: `company`, `name`, `code`, `address`, `cnpj`, `state_registration`, `registration_hash`, `active`, `chain_unit_id` (nulo até confirmação). O CNPJ normalizado é obrigatório e único entre todas as unidades do SaaS, inclusive inativas. O SaaS normaliza os campos cadastrais, monta um JSON determinístico com `name`, `code`, `address`, `cnpj` e `state_registration`, calcula Keccak-256 compatível com Ethereum e grava o resultado em `registration_hash`; essa hash é enviada como `registrationHash` ao registrar ou atualizar a unidade. `active`, IDs internos, `chain_unit_id` e horários não entram na hash. Código único por empresa. Identificador da unidade na rede único por instância do registro de unidades.
- **UnitAccess**: `membership`, `unit`, `can_view`, `can_receive`, `can_issue`, `can_audit`, `active`. A unidade e o vínculo do usuário devem pertencer à mesma empresa. A função e a concessão para a unidade são verificadas em conjunto; a permissão no SaaS não substitui a permissão da carteira no contrato.
- **UnitWalletPermission**: `unit`, `wallet`, `permission_mask`, `transaction` (última transação de configuração), `sync_status`. Representa o último estado confirmado ou pendente das permissões na blockchain; `is_effective` só é verdadeiro quando há confirmação e hash de transação.
- **AccessChange**: `company`, `membership`, `unit` opcional, `actor`, `action`, `previous_value`, `new_value`, `transaction` opcional, `occurred_at`. Histórico imutável das concessões e revogações no SaaS e das mudanças de carteira acompanhadas na rede.

### `blockchain`

- **Network**: `name`, `chain_id`, `rpc_url`, `active`. Cada rede salva seu endpoint RPC no banco; conexões usam o endpoint e conferem o `chain_id` do mesmo registro. O cadastro e a leitura da URL ficam restritos a superusuários, pois a URL pode conter credenciais do provedor.
- **ContractInstance**: `company`, `network`, `kind` (SojaCoin, registro de unidades ou lotes), `address`, `address_normalized`, `version`, `deployment_transaction_hash`, `active`. Endereço único por rede; apenas uma instância ativa de cada tipo por empresa e rede no MVP. Conferir os vínculos reais entre contratos antes de ativar a configuração.
- **BlockchainTransaction**: `company`, `network`, `contract`, `operation_type`, `sender_address`, `transaction_hash` (nulo antes do envio), `status` (preparada, enviada, confirmada, falhou ou confirmação revertida), `block_number`, `block_hash`, `submitted_at`, `confirmed_at`, `error_message`. Hash único por rede quando preenchido. `created_at` é o registro local; `confirmed_at` representa a confirmação observada.
- **ProcessedEvent**: `network`, `contract`, `transaction_hash`, `log_index`, `block_number`, `block_hash`, `event_name`, `processed_at`. Restrição única para rede + hash da transação + índice do evento, permitindo processar novamente sem duplicar efeitos. Confirmar novamente após reorganização da rede.

### `documents`

- **Document**: `company`, `document_type`, `original_filename`, `storage_key`, `content_type`, `size_bytes`, `hash_algorithm`, `file_hash`, `uploaded_by`, `uploaded_at`. Controlar acesso por empresa e guardar versões separadamente para não sobrescrever um documento já associado a uma operação.
- **DocumentLink**: `document`, `content_type`, `object_id`, `purpose`, `linked_by`. Associa um documento a uma CPR, auditoria ou carga; validar que documento e operação pertencem à mesma empresa. Quando uma operação exigir um hash específico, guardar explicitamente esse valor no modelo da operação.

### `participants`

- **Participant**: `company`, `name`, `person_type`, `tax_id`, `email`, `phone`, `wallet`, `is_producer`, `is_creditor`, `active`. Uma mesma pessoa pode ter ambos os papéis. Restringir duplicidade de carteira por empresa e tratar CPF/CNPJ com acesso controlado.
- **ParticipantUser**: `participant`, `user`, `active`, `granted_by`. Vínculo explícito para acesso aos próprios registros; o usuário deve comprovar controle da carteira ao assinar operações, ou usar o fluxo adequado para carteira contratual. Restringir duplicidade por participante e usuário.
- **ParticipationTerms**: `company`, `participant`, `document`, `terms_hash`, `status` (proposto, aceito, cancelado), `accepted_at`, `transaction`. Confirmar a assinatura e o aceite na rede antes de mudar para aceito.

### `audits`

- **UnitAudit**: `company`, `unit`, `auditor_wallet`, `document`, `report_hash`, `physical_inventory_kg`, `audited_at`, `transaction`, `status`. `audited_at` é o horário confirmado na rede; `created_at` é quando o registro foi iniciado no SaaS. O estoque agregado do contrato é registrado em mg; converter kg para mg com exatidão.

### `cprs`

- **CPR**: `company`, `producer`, `current_creditor`, `delivery_unit`, `crop_year`, `crop_year_bytes32`, `initial_amount_sjc_units`, `outstanding_amount_sjc_units`, `priority`, `due_at`, `document`, `document_hash`, `producer_accepted_at`, `chain_cpr_id`, `status`, `registration_transaction`. Estados exibidos: rascunho, aguardando assinatura, pendente na rede, aberta, vencida com saldo, quitada, encerrada por acordo e cancelada. Os estados de rascunho e pendência são locais; os demais refletem o contrato.
- **CPRRepayment**: `company`, `cpr`, `receipt`, `creditor`, `amount_sjc_units`, `overdue`, `transaction`, `event_log_index`. Restrição de unicidade pelo evento que originou o abatimento; guardar o credor que recebeu naquela ocasião para preservar o histórico.

O campo `crop_year_bytes32` deve seguir uma convenção única e reproduzível na CPR e na carga; o texto exibido ao usuário pode ser, por exemplo, `2026/2027`. O prazo da assinatura é diferente do vencimento da CPR e deve ser guardado na solicitação de assinatura.

### `receipts`

- **Receipt — cadastro local implementado**: `company`, `participant`, `unit`, `crop_year`, `crop_year_bytes32`, `status`, `created_by`, timestamps. A safra em bytes32 é UTF-8 preenchido à direita com zeros. O status local é `ready_for_blockchain`; ele não significa que uma transação tenha sido enviada.
- **WeighingTicket — cadastro local implementado**: `receipt` (um para um), `gross_weight_kg`, `tare_weight_kg`, `net_weight_kg`, `moisture_bps`, `impurities_bps`, `damaged_grain_bps`, `moisture_deduction_kg`, `impurities_deduction_kg`, `damaged_grain_deduction_kg`, `other_deductions_kg`, `weighing_ticket_hash`, `report_hash`, `weighed_at`. O servidor calcula peso líquido; os índices são centésimos de percentual, por exemplo 1.550 representa 15,50%.
- **ReceiptDocument — cadastro local implementado**: `receipt`, `kind`, nome original, arquivo PDF privado, hash Keccak-256 dos bytes exatos, tamanho, responsável pelo envio e timestamps. Há no máximo um documento de cada tipo por recebimento: romaneio, laudo de classificação e nota fiscal. O romaneio e a NF-e são obrigatórios no formulário; o laudo é opcional.
- **TaxInvoice — cadastro local implementado**: `receipt`, chave de acesso de 44 dígitos, `access_key_hash` e documento. A hash destinada ao contrato é calculada sobre a chave de acesso; ela é diferente da hash do PDF da nota.
- **Envio e confirmação na blockchain — pendentes**: ainda precisam ser implementados `chain_deposit_id`, preparação e confirmação de `openLoadReceipt`, link da nota fiscal, `confirmLoadReceipt`, transações associadas e sincronização de eventos. A próxima etapa deve validar rede, contrato, carteira e permissão antes de solicitar assinatura.

### `issuance`

- **IssuanceBatch**: `company`, `producer`, `chain_batch_id`, `status`, `deadline_at`, `summary_hash`, `created_by`, `creation_transaction`, `approval_transaction`, `approved_at`. O prazo limita a assinatura; o estado do lote acompanha a rede.
- **IssuanceBatchReceipt**: `batch`, `receipt`, `issued_at`, `issuance_transaction`. Restrição única para `batch` + `receipt`; validar empresa e produtor e impedir incluir uma carga em lote ativo incompatível conforme o contrato.
- **SignatureRequest**: `company`, `operation_type`, `expected_wallet`, `payload_version`, `digest`, `deadline_at`, `status` (pendente, assinada, expirada ou cancelada), `signed_at`, `signature`, `transaction`. O vínculo com termo, CPR ou lote deve ser explícito e validado: exatamente uma operação por solicitação. Registrar os dados necessários para reconstruir o conteúdo assinado e evitar reutilização em outra rede, contrato ou operação.

## Integração com carteiras e rede

- O navegador conecta a carteira e solicita a assinatura; o Django não recebe a chave privada. As mensagens assinadas devem corresponder exatamente aos dados e ao domínio esperados pelo contrato. Verificar carteira e rede selecionadas antes de pedir a assinatura.
- A interface mostra documento, quantidade, credor, armazém, safra e prazo relevantes antes da assinatura. Se a pessoa recusar ou o prazo vencer, mostrar a pendência e permitir reiniciar quando cabível.
- Registrar o envio, acompanhar confirmação e falha, e sincronizar os eventos de forma repetível. Conciliar registros locais com o contrato após interrupção ou reorganização de blocos.
- Para uma Safe, o endereço corporativo pode ser participante ou proprietário quando o contrato e o fluxo de assinatura forem compatíveis. A integração específica com aprovações da Safe deve ser avaliada antes de incluí-la no MVP; MetaMask individual não substitui automaticamente assinaturas exigidas pela Safe.

## Ordem de construção

Esta é a sequência de implementação. Concluir e verificar cada etapa antes de seguir. Os itens já criados em `companies` serão aproveitados na etapa 4.

1. **Base visual e arquivos estáticos — concluída.** Tailwind e seu CLI estão instalados em `app`; `templates/base.html` referencia o CSS gerado. O CSS de entrada, os comandos de geração, os diretórios estáticos e a coleta para publicação estão configurados. O Django encontrou o CSS e `collectstatic` reuniu os arquivos; os comandos estão documentados no README de `app`.
2. **Contas assistidas, login e logout — implementados.** A equipe da plataforma cria as contas iniciais no Django Admin e vincula explicitamente os usuários aos registros apropriados. Login, logout e perfil usam o usuário padrão; não há cadastro público. A associação aos registros apropriados continua sendo um procedimento assistido.
3. **Acesso por empresa — pendente.** Criar as verificações de usuário ativo, empresa ativa, vínculo e função; negar acesso a dados de outra empresa. Usar os modelos existentes de `companies`. Verificar com usuários de duas empresas e vínculos inativos.
4. **Empresa e unidade no SaaS — parcial.** Os modelos e o serviço de hash da unidade já existem. A equipe da plataforma cadastra a empresa e o vínculo inicial do administrador de forma assistida. Criar formulários, rotas e telas para os dados que o administrador poderá gerir. O cadastro e a edição da unidade devem usar o serviço de hash, inclusive se forem disponibilizados pelo Admin. Verificar CNPJ único, hash e isolamento entre empresas. Nesta etapa o cadastro é local; ele não confirma registro na blockchain.
5. **Equipe e permissões do SaaS — concluída para o escopo atual.** Os fluxos de vínculo, concessão e revogação de funções, acessos por unidade e consulta do histórico imutável estão implementados e testados. Cada alteração registra responsável, empresa, membro, unidade quando aplicável, valores anterior e novo, horário e transação quando existir. Permissões do SaaS não alteram a carteira na rede.
6. **Rede e contratos — parcial.** `blockchain` já possui os modelos de rede, contratos, transações e eventos, além das transições de confirmação, falha, reorganização e processamento idempotente. Ainda falta conectar o RPC, acompanhar recibos e decodificar os eventos dos contratos. A implantação, conexão e aprovações da Safe são assistidas pela equipe. Verificar os vínculos entre contratos, status de confirmação e falhas.
7. **Carteiras, participantes e documentos — parcial.** Em `accounts`, perfil, carteira, vínculo usuário–carteira e verificação por assinatura estão implementados. `participants` cobre publicação pela empresa de PDF versionado e imutável, cadastro genérico de participante, proposta do dono da SojaCoin, link individual, aceite EIP-712 e confirmação do contrato. Produtores e credores podem ser representados como participantes; os fluxos específicos de CPR e crédito continuam pendentes. O SaaS registra a declaração de aprovação feita pelo administrador, mas não revisa nem certifica juridicamente o conteúdo. `receipts` armazena em área privada os PDFs do romaneio, laudo opcional e nota fiscal ligados ao recebimento local. Os documentos específicos de CPR e outras operações ainda estão pendentes. Não guardar chaves privadas.
8. **Unidades na rede e auditorias — pendente.** Conectar o cadastro da unidade, designação de auditor e permissões à carteira proprietária apropriada; registrar auditorias e confirmar os resultados da rede antes de exibi-los como efetivos.
9. **CPR — pendente.** Criar cadastro, documento, assinatura do produtor e acompanhamento da transação e do saldo da CPR.
10. **Recebimento — cadastro local concluído; integração blockchain pendente.** O usuário com função de recebimento e acesso ativo à unidade registra participante confirmado, unidade já registrada, safra, pesagem/classificação, chave da NF-e e PDFs privados. O servidor calcula o peso líquido e as hashes. O envio de `openLoadReceipt`, vínculo da nota, confirmação da carga e confirmação de recibos ainda não estão ligados à interface.
11. **Lote e emissão — pendente.** Criar lote, solicitar assinatura do produtor, enviar aprovação e encaminhar emissão com carteira autorizada. Acompanhar as duas transações separadamente.
12. **Consultas e verificação completa — pendente.** Criar consultas de saldos, abatimentos e histórico por perfil; exibir transações e eventos sem duplicidade; testar o fluxo completo em rede de teste e os casos de recusa, erro, reorganização e isolamento. A rede local pode ser usada durante o desenvolvimento.

Durante a construção, cobrir também erros, permissões, recusas de assinatura, transações pendentes e duplicidade de eventos. Começar com SQLite local; preparar PostgreSQL e armazenamento de documentos persistente para o ambiente de implantação.

## Fora do primeiro fluxo

Implantação automática dos contratos, interface completa de Safe, cessão e reordenação de CPR, administração de múltiplas CPRs pela tela, transferência de estoque entre unidades, cobranças de serviços e resgate físico ficam para etapas posteriores. O contrato pode conter recursos que ainda não estarão expostos nas telas.

A retirada física fica fora do primeiro fluxo: o MVP demonstra recebimento, abatimento e emissão, mas não cobre a retirada de soja pelo credor na interface.

## Critérios de aceitação do MVP

Em rede de teste, executar uma CPR de 400 sacas e uma entrega de 500 sacas líquidas. Após a emissão confirmada, verificar 400 SJC para o credor, 100 SJC para o produtor e CPR quitada, conferindo também o contrato e os eventos. Os valores equivalem a 400.000.000 e 100.000.000 unidades mínimas de token, respectivamente.

Verificar também acesso entre empresas recusado, usuário sem vínculo, carteira ou rede incorreta, assinatura recusada, transação pendente ou com falha e ausência de emissão antes da confirmação. Verificar os arquivos e hashes usados na operação. Os testes devem usar os contratos e carteiras de teste, sem solicitar chaves privadas na aplicação.
