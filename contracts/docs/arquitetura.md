# Arquitetura atual do contrato

## Objetivo desta etapa

`SojaCoin.sol` mantém o token, o armazenamento das operações e o endereço principal
que os usuários e outros contratos chamam. Para ficar abaixo do limite de bytecode,
as regras de negócio são executadas por seis contratos de lógica implantados à parte.
`UnitRegistry.sol` mantém units, permissões, auditorias e transferências físicas;
`TokenIssuanceBatches.sol` guarda as aprovações dos lotes.

Todos os contratos `SojaCoin...` de lógica são módulos fixos. O SojaCoin chama esses
módulos com `delegatecall`: a lógica roda usando o armazenamento e o endereço do
SojaCoin, então saldos, eventos e operações continuam pertencendo ao contrato principal.
As implementações são definidas no construtor e não podem ser trocadas depois.
O construtor também confere o identificador de cada módulo para evitar trocar as
funções de lugar por engano. Use os seis endereços dos contratos corretos desta
mesma versão do projeto.

| Arquivo | Responsabilidade |
| --- | --- |
| `storage/SojaCoinStorage.sol` | Estruturas, estados, constantes e eventos compartilhados. |
| `modules/TokenModule.sol` | SJC, pausa, controle de movimentação, conversão de units e rotinas internas usadas por travas e resgates. |
| `modules/AdministrationModule.sol` / `SojaCoinAdministration` | Participantes, lastro, pausa e conexões administrativas. |
| `modules/DepositsModule.sol` / `SojaCoinDeposits` | Depósitos, romaneios e emissão. |
| `modules/GuaranteesModule.sol` / `SojaCoinCPR` | CPRs como dívidas em sacas, abatimentos, cessão e priority. |
| `modules/TokenIssuanceBatches.sol` | Agrupa cargas confirmadas e valida a aprovação do producer para o lote. |
| `modules/LocksModule.sol` / `SojaCoinLocks` | Garantias feitas com SJC que o holder já possui. |
| `modules/ServicesModule.sol` / `SojaCoinServices` | Serviços e tarifas cobrados pelo armazém. |
| `modules/RedemptionsModule.sol` / `SojaCoinRedemptions` | Solicitação, autorização, conclusão e arbitragem de retiradas. |
| `UnitRegistry.sol` | Unidades físicas, auditor responsável por unit, permissões e transferência física entre units. |

## Relação entre os módulos

Ao emitir SJC por um depósito, `DepositsModule` chama `SojaCoinCPR`. Esse módulo abate as dívidas compatíveis e cria SJC diretamente para cada credor; o excedente vai ao producer. Os módulos usam a mesma base de armazenamento do SojaCoin.

## Ligação entre os contratos

Implante nesta ordem:

1. `SojaCoinAdministration`, `SojaCoinDeposits`, `SojaCoinCPR`, `SojaCoinServices`, `SojaCoinLocks` e `SojaCoinRedemptions`. Não recebem argumentos.
2. `SojaCoin`, passando os argumentos nesta ordem: `initialOwner`, endereço de `SojaCoinAdministration`, endereço de `SojaCoinDeposits`, endereço de `SojaCoinCPR`, endereço de `SojaCoinServices`, endereço de `SojaCoinLocks` e endereço de `SojaCoinRedemptions`. Em produção, `initialOwner` pode ser o endereço de uma Safe corporativa.
3. `UnitRegistry`, com o dono e o endereço do `SojaCoin` implantado.
4. `TokenIssuanceBatches`, com o endereço do `SojaCoin` implantado.
5. Pelo `SojaCoin`, o dono chama `configureUnitRegistry` e `configureIssuanceBatches`.
6. Cadastra as units, define o auditor responsável de cada uma e registra as auditorias individualmente.

As operações do dia a dia são chamadas no endereço do `SojaCoin`, nunca nos endereços
dos seis módulos de lógica. Os módulos não guardam os saldos nem os dados da empresa.

Cada unit tem um auditor responsável definido pelo dono. Esse auditor registra o
estoque e o laudo somente da sua unit. O saldo geral é atualizado pela diferença
da nova contagem. A auditoria global fica válida quando todas as units ativas têm
laudo dentro do prazo; a data considerada é a auditoria mais antiga entre elas.

## Depósito e emissão por unit

1. O administrador propõe o cadastro com o hash do termo. A wallet do producer
   assina o aceite uma única vez; somente então ela vira participante e pode ter
   cargas tokenizadas. O termo pode cobrir todas as units da empresa; o producer
   pode revogar essa autorização para cargas futuras.
2. Quando o caminhão chega, um funcionário com permissão de recebimento chama
   `openLoadReceipt` informando o producer e a unit. O contrato gera o ID do
   depósito e o registra no evento `LoadReceiptOpened`. O funcionário, não o
   producer, envia a transação por carga.
3. A nota fiscal é vinculada por `linkTaxInvoice`. Isso pode ocorrer antes ou
   depois do weighingTicket; a carga não pode entrar em lote ou emitir SJC sem essa etapa.
4. Um funcionário com permissão de recebimento nessa unit confirma o weighingTicket.
   O peso líquido aceito entra no estoque local e no lastro global, sem duplicar a
   contagem entre os contratos.
5. Antes da emissão, o armazém pode registrar uma devolução física parcial com o
   hash do documento de saída. Essa amount sai do estoque e não gera SJC.
6. Outro funcionário, com permissão de emissão, reúne cargas confirmadas do mesmo producer
   no contrato `TokenIssuanceBatches`. O contrato gera o ID do lote e o registra no evento
   `BatchCreated`; o lote fixa os IDs, pesos e hashes dos romaneios e documentos.
7. O producer assina uma única autorização EIP-712 para aquele lote. A assinatura tem
   prazo e nonce, portanto não pode ser reutilizada em outro lote nem depois do dueDate.
8. Um funcionário com permissão de emissão chama `issueTokens` para cada carga aprovada.
   O SojaCoin consulta `TokenIssuanceBatches` antes de emitir. Na emissão, abate as CPRs compatíveis por priority e envia SJC diretamente aos credores atuais; a sobra vai ao producer.

Cobranças de serviços ligadas ao depósito também usam a permissão de gestão de
serviços daquela unit. Elas continuam separadas do peso líquido e da emissão.

O contrato mantém o hash do termo do participante e exige autorização ativa para
registrar novos depósitos. A revogação bloqueia depósitos novos, mas não cancela
recebimentos já registrados nem impede o participante de usar SJC que já possui.

Enquanto uma carga está em um lote aguardando assinatura ou aprovado, ela não pode
ser emitida sem a aprovação nem receber devolução parcial. Se houver erro, o producer,
quem criou o lote, o producer ou o administrador pode cancelar o lote antes da
aprovação; as cargas voltam a ficar disponíveis para correção ou para outro lote.
Após a assinatura, a lista não pode ser alterada.

Os pesos operacionais de depósito e devolução são informados em kg inteiros. Os
documentos ficam fora da blockchain; o contrato guarda seus hashes e os responsáveis.
Se o depósito for canceled antes do recebimento, o histórico e o ID permanecem
reservados, mas nenhum estoque é alterado.

## Retirada por unit

1. O holder pede a retirada e escolhe uma unit. O contrato verifica se ela tem
   estoque disponível e mantém os SJC do pedido em custódia.
2. Um funcionário com permissão de autorização confirma o pedido. Nesse momento, o
   peso correspondente fica reservado na unit e deixa de estar disponível para
   outra retirada ou transferência.
3. Na saída, outro funcionário, com permissão de conclusão, registra o peso efetivamente
   entregue em kg e o hash do documento fiscal. A wallet que autorizou não pode concluir
   a mesma retirada. O contrato queima SJC equivalentes
   ao peso entregue e devolve ao holder os SJC que sobraram do pedido.
4. Se nada for entregue, o pedido autorizado pode ser canceled: a reserva da unit
   é liberada e todos os SJC retornam ao holder. Se o armazém não resolver no prazo,
   permanece a arbitragem do dono já prevista no contrato.

O saldo de SJC continua fungível na empresa; escolher uma unit define o local de
atendimento físico, não marca os tokens com uma unit específica.

## Implantação desta versão

As estruturas e assinaturas públicas mudaram, e o projeto não usa proxy atualizável.
Para usar esta versão, implante os seis módulos fixos, depois `SojaCoin`, `UnitRegistry`
e `TokenIssuanceBatches`, seguindo a ordem acima. Conecte os auxiliares, cadastre as units
e configure as permissões. A função `openLoadReceipt` é chamada pelo funcionário
autorizado e recebe explicitamente a wallet do producer; não usa uma assinatura
EIP-712 do armazém por caminhão.

Esta atualização não migra dados nem saldos automaticamente. O contrato antigo continua
na rede com seu estado e suas regras antigas; CPRs e SJC que já estejam nele não passam
para a nova implantação. Antes de trocar de versão, a empresa precisa reconciliar as
operações e decidir como tratar esses saldos fora desta mudança de código.

## CPR como dívida em sacas

Cada CPR registra producer, credor atual, valor inicial, saldo devedor, unit de
entrega, cropYear, dueDate, priority, hash do documento e aceite assinado pelo
producer. O documento completo fica fora da blockchain.

Quando uma carga gera SJC, o contrato considera somente CPRs do mesmo producer,
unit e cropYear. A dívida mais prioritária recebe primeiro; se ela for quitada,
o restante segue para a próxima. O SJC de cada abatimento é criado diretamente
para o credor daquela CPR. Depois de toda a fila compatível, o excedente é criado
para o producer. Uma carga recebida em outra unit só abate a CPR com aceite
assinado do producer e do credor atual para aquela carga.

Uma CPR vencida que ainda tenha saldo muda para `OverdueWithBalance`. Entregas posteriores
ainda podem reduzir a dívida e ficam marcadas como atrasadas. O dueDate, por si só,
nunca cria SJC. Um weighingTicket só pode ser usado uma vez.

A priority inicial é assinada pelo producer no cadastro. Uma mudança posterior exige
novo aceite do producer e dos credores das CPRs que mudariam de posição. O evento guarda
quem pediu, quem aceitou, o horário e o hash do documento.

Na cessão, o credor atual pode enviar a operação diretamente ou assinar uma autorização
para outra wallet enviá-la. É obrigatório informar uma referência de registro e o hash
do documento. O contrato não consulta a registradora nem confirma se a cessão aconteceu
oficialmente; ele guarda a prova informada e direciona as próximas entregas ao novo credor.
O evento `CPRAssigned` permite ao SaaS avisar o producer.

Cancelar uma CPR só é permitido antes de qualquer abatimento. Para encerrar por acordo
uma CPR com saldo, producer e credor atual assinam o encerramento; o saldo restante é
marcado como encerrado, sem criar tokens.

## Transferência física entre units

1. Um funcionário com permissão na origem solicita uma carga. O peso solicitado fica reservado. Cada ID representa uma carga; use IDs separados para cargas diferentes.
2. No despacho, o estoque sai da origem e passa para o saldo em trânsito.
3. Um funcionário com permissão no destino confirma o peso recebido. O contrato baixa o peso despachado do trânsito e credita o peso recebido ao destino.
4. Se o peso recebido for menor, a diferença é registrada e reduz o estoque global. O SojaCoin pausa se o estoque restante não cobrir os tokens emitidos.

As funções recebem kg e convertem para miligramas internamente. O saldo em trânsito
entra no lastro global e nas auditorias por unit. Se a carga chegar com peso menor,
o contrato registra o peso despachado, o recebido, a diferença, o responsável e o hash
do comprovante; a diferença reduz o estoque físico global registrado. Ela não é
descontada do saldo de um producer, pois a transferência registra estoque agregado da
empresa, sem rastrear lotes de um producer específico. A transferência não cria, queima
ou transfere SJC. Os documentos completos ficam fora da blockchain; o registro guarda
seus hashes.
