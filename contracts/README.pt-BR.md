# SojaCoin

[English](README.md) | **Português**

Contrato para registrar recebimentos de soja e emitir SJC lastreado no estoque auditado. **1 SJC representa uma saca de 60 kg**. O token tem seis casas decimais para representar frações de saca.

Os documentos ficam fora da blockchain; o contrato registra seus hashes e as etapas da operação. Uma implantação representa uma empresa. As units identificam onde a soja física foi recebida ou será retirada; os saldos de SJC permanecem fungíveis dentro da empresa.

## Roteiro completo em rede local

Este roteiro usa Anvil e a extensão Solidity (Wake) do VS Code. Ele percorre a implantação até a emissão de SJC para uma carga. Os endereços e hashes abaixo são exemplos de teste.

### 1. Inicie a rede local e compile

No terminal, inicie o Anvil:

```bash
~/.foundry/bin/anvil --host 127.0.0.1 --port 8545 --gas-price 0 --block-base-fee-per-gas 0
```

No Wake, conecte a uma rede existente:

```text
RPC: http://127.0.0.1:8545
Chain ID: 31337
```

Use **Compile all** no Wake. Mantenha o terminal do Anvil aberto durante o teste.

Para cada chamada abaixo, abra **Interact**, escolha a instância indicada, expanda a função, selecione a conta em **Sender Account**, preencha os argumentos na ordem mostrada e clique em **Call**. Para implantar, selecione o contrato em **Deploy Contracts** e preencha os argumentos do construtor, quando houver.

As contas padrão do Anvil são:

| Conta | Uso neste roteiro | Endereço |
| --- | --- | --- |
| Account0 | Administrador da empresa e contratos | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| Account1 | Funcionário que recebe e confirma cargas da unit 1 | `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` |
| Account2 | Auditor da unit 1 | `0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC` |
| Account3 | Auditor da unit 2 | `0x90F79bf6EB2c4f870365E785982E1f101E93b906` |
| Account4 | Produtor | `0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65` |
| Account5 | Funcionário que cria lotes e emite SJC da unit 1 | `0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc` |

As chaves dessas contas são públicas e servem somente para a rede local. Nunca use essas chaves com ativos reais.

### 2. Implante os módulos e o SojaCoin

No Wake, implante estes seis módulos de lógica. Eles não recebem argumentos:

1. `SojaCoinAdministration`
2. `SojaCoinDeposits`
3. `SojaCoinCPR`
4. `SojaCoinServices`
5. `SojaCoinLocks`
6. `SojaCoinRedemptions`

Em seguida, implante `SojaCoin` com os argumentos nesta ordem:

1. `initialOwner`: Account0
2. endereço da implantação nova de `SojaCoinAdministration`
3. endereço da implantação nova de `SojaCoinDeposits`
4. endereço da implantação nova de `SojaCoinCPR`
5. endereço da implantação nova de `SojaCoinServices`
6. endereço da implantação nova de `SojaCoinLocks`
7. endereço da implantação nova de `SojaCoinRedemptions`

Em produção, `initialOwner` pode ser o endereço de uma Safe da empresa. O SojaCoin
não cria nem administra a Safe: ele apenas reconhece o endereço dela como administrador.

Copie cada endereço exibido em **Deployed to** no Wake. Use módulos da mesma compilação.

### 3. Implante e conecte os contratos auxiliares

Depois do SojaCoin, implante:

- `UnitRegistry(initialOwner, sojaCoinAddress)`: Account0 e o endereço do SojaCoin recém-implantado.
- `TokenIssuanceBatches(sojaCoinAddress)`: o mesmo endereço do SojaCoin.

No SojaCoin, com Account0 como remetente, chame `configureUnitRegistry(unitRegistryAddress)` e `configureIssuanceBatches(issuanceBatchesAddress)`.

Confira os vínculos antes de continuar: `SojaCoin.unitRegistry()` deve mostrar o UnitRegistry usado; `SojaCoin.issuanceBatchModule()` deve mostrar o TokenIssuanceBatches usado; e `TokenIssuanceBatches.sojaCoin()` deve mostrar o SojaCoin atual. Não misture endereços de implantações antigas.

### 4. Cadastre units, auditores e estoque inicial

No `UnitRegistry`, com Account0, cadastre duas units. As hashes são fictícias para o teste:

```text
registerUnit(0x41524d415a454d2d303100000000000000000000000000000000000000000000, 0xfedef253c88d99aa89049b979b605d2526f7bd389d6993fb0956ba69a5a26ee1)
registerUnit(0x41524d415a454d2d303200000000000000000000000000000000000000000000, 0x2222222222222222222222222222222222222222222222222222222222222222)
```

Os códigos representam `ARMAZEM-01` e `ARMAZEM-02`. Os IDs retornados devem ser `1` e `2`.

Associe os auditores com Account0:

```text
setUnitAuditor(1, 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC)
setUnitAuditor(2, 0x90F79bf6EB2c4f870365E785982E1f101E93b906)
```

Cada auditor registra somente a unit que lhe foi atribuída. Os estoques são em kg inteiros; as hashes são fictícias:

```text
# remetente Account2
recordUnitAudit(1, 85000, 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
# remetente Account3
recordUnitAudit(2, 150000, 0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)
```

No campo **Sender Account** do Wake, selecione a conta indicada em cada comentário. Para os outros campos de endereço deste roteiro, use os endereços da tabela acima.

Consulte `allUnitAuditsCurrent()`. Deve retornar `true`. O estoque agregado deste exemplo é 235.000 kg, ou `235000000000` mg.

Dê a Account1 apenas a permissão de recebimento da unit 1. Depois, dê a
Account5 apenas a permissão de emissão. O contrato exige essas carteiras distintas
na mesma unit.

No UnitRegistry, com Account0, chame:

```text
setPermissions(1, 0x70997970C51812dc3A010C7d01b50e0d17dc79C8, 1)
setPermissions(1, 0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc, 2)
```

O valor `1` é receber soja e o valor `2` é emitir SJC. Configure permissões
separadas para cada unit em que os funcionários vão operar.

### 5. Cadastre o producer

No SojaCoin, com Account0 como remetente, proponha o cadastro da Account4. A hash
abaixo é fictícia:

```text
proposeParticipantRegistration(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 0x5555555555555555555555555555555555555555555555555555555555555555)
```

Defina um `deadline` futuro, por exemplo `2000000000`. Consulte no SojaCoin:

```text
participationTermsAcceptanceHash(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 2000000000)
```

Copie o digest retornado. A Account4 assina esse digest no terminal do Anvil:

```bash
~/.foundry/bin/cast wallet sign --no-hash "COLE_O_DIGEST_AQUI" --private-key "COLE_A_CHAVE_PRIVADA_LOCAL_DA_ACCOUNT4_AQUI"
```

Por fim, qualquer conta pode enviar a assinatura ao SojaCoin:

```text
acceptParticipationTerms(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 2000000000, COLE_A_ASSINATURA_DA_ACCOUNT4)
```

Na operação real, essa hash identifica o termo final guardado fora da blockchain.
A assinatura prova que a própria wallet do participante aceitou o termo. O SaaS pode
enviar a última transação sem guardar a chave privada do producer.

### 6. Abra e confirme o recebimento da carga

O caminhão chega à unit 1. No SojaCoin, selecione Account1 e chame. O terceiro argumento é o identificador bytes32 da safra; use o mesmo valor ao registrar CPRs para essa safra:

```text
openLoadReceipt(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 1, 0x43524f502d323032362f32303237000000000000000000000000000000000000)
```

O contrato gera o ID do depósito. Depois da transação, pegue o valor no evento
`LoadReceiptOpened` (ou consulte `nextDepositId`) e use esse ID em chamadas como
`confirmLoadReceipt`, `linkTaxInvoice` e `issueTokens`.
Neste roteiro com uma implantação nova, o primeiro ID é `1`.

Confirme o weighingTicket com Account1. Este exemplo registra 25.500 kg brutos, 300 kg de tara e 450 kg de desconto. Como a data do weighingTicket deve ser posterior à auditoria da unit e não pode estar no futuro, no terminal WSL execute `date +%s` depois das auditorias e use o número retornado como `timestamp`. Se a auditoria acabou de ser registrada no mesmo segundo, aguarde um segundo e consulte de novo.

```text
confirmLoadReceipt(depositId=1, weighingTicket=SojaCoin.WeighingTicket(grossWeightKg=25500, taraKg=300, moistureBps=1550, impuritiesBps=0, damagedGrainBps=0, moistureDeductionKg=450, impuritiesDeductionKg=0, damagedGrainDeductionKg=0, otherDeductionsKg=0, weighingTicketHash=0x3333333333333333333333333333333333333333333333333333333333333333, reportHash=0x4444444444444444444444444444444444444444444444444444444444444444, timestamp=COLE_AQUI_O_RESULTADO_DE_date_+%s))
```

O peso líquido é 24.750 kg. Como o weighingTicket é posterior à auditoria, a unit 1 passa de 85.000 kg para 109.750 kg e o estoque agregado da empresa passa de 235.000 kg para 259.750 kg.

Depois que a nota fiscal estiver disponível, vincule-a à carga com Account1. A hash abaixo é fictícia:

```text
linkTaxInvoice(1, 0x1111111111111111111111111111111111111111111111111111111111111111)
```

A nota pode ser vinculada antes ou depois do weighingTicket, mas precisa estar vinculada antes da criação do lote.

### 7. Crie e aprove o lote

No `TokenIssuanceBatches`, com Account5, agrupe o depósito 1. O prazo deve ser um timestamp Unix futuro em relação ao bloco atual; `2000000000` serve para esta simulação enquanto continuar no futuro:

```text
createIssuanceBatch([1], 2000000000)
```

O contrato gera o ID do lote. Depois da transação, pegue o valor no evento `BatchCreated`
(ou consulte `nextBatchId`) e use esse ID em `issuanceBatchAuthorizationHash` e
`approveIssuanceBatch`.
Neste roteiro com um contrato de lotes novo, o primeiro ID também é `1`.

Consulte `issuanceBatchAuthorizationHash(1)` no TokenIssuanceBatches e copie o digest. O producer, Account4, assina o digest com a chave privada local mostrada no terminal do Anvil:

```bash
~/.foundry/bin/cast wallet sign --no-hash "COLE_O_DIGEST_AQUI" --private-key "COLE_A_CHAVE_PRIVADA_LOCAL_DA_ACCOUNT4_AQUI"
```

Envie a assinatura retornada em `approveIssuanceBatch(1, assinatura)` no TokenIssuanceBatches. A transação pode ser enviada por Account0, Account1 ou outra wallet; a assinatura precisa ser do producer. No SaaS, o producer assina uma vez e a aplicação pode enviar essa chamada à blockchain em seguida.

### 8. Emita e confira os SJC

Depois da aprovação, no SojaCoin, com Account5 como remetente, chame:

```text
issueTokens(1)
```

Com 24.750 kg líquidos e sem CPR cadastrada, o producer recebe 412,5 SJC. Como `balanceOf` retorna units inteiras e `decimals()` é 6, o resultado será `412500000`, equivalente a 412,5 SJC. Consulte:

```text
balanceOf(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65)
```

Se houver CPRs abertas do mesmo producer, unit e cropYear, a emissão paga essas dívidas na ordem aprovada. O SJC de cada abatimento vai diretamente ao credor atual da CPR. Só o excedente, depois da fila compatível, vai ao producer. Exemplo: dívida de 7.000 sacas e entrega de 8.000: 7.000 SJC vão ao credor, 1.000 SJC vão ao producer e a CPR fica quitada.

As quantidades de CPR usam as units internas do token. Como `decimals()` é 6, 7.000 SJC são informados como `7000000000` units. O contrato reduz o saldo devedor a cada emissão; não mantém SJC da CPR presos no contrato.

### Testes automatizados

Com Foundry instalado, rode os testes da CPR na raiz do projeto:

```bash
forge test --match-path test/SojaCoinCPR.t.sol
```

Os testes cobrem abatimentos integrais e parciais, fila de CPRs, compatibilidade por unit e cropYear, autorização de unit alternativa, aceite de priority, cessão, atraso, cancelamento, encerramento por acordo e weighingTicket repetido.

## Observações

- Todas as hashes preenchidas com `1111`, `2222`, `3333`, `4444`, `5555`, `aaaa` ou `bbbb` neste roteiro são fictícias. Em uma operação real, a aplicação calcula a hash do arquivo guardado fora da blockchain.
- Cada unit ativa precisa ter auditor responsável e auditoria vigente para liberar emissão.
- O lote deve ser criado no mesmo `TokenIssuanceBatches` conectado ao SojaCoin atual. Se `createIssuanceBatch` reverter, confira os getters de endereço da etapa 3.
- Os endereços dos módulos ficam fixos no SojaCoin e os contratos auxiliares apontam para uma implantação específica. Após alterar o código, implante uma nova versão e refaça os cadastros na nova implantação.

Mais detalhes estão em [docs/arquitetura.md](docs/arquitetura.md) e [LOGICA_ENTREGA_SOJA.md](LOGICA_ENTREGA_SOJA.md).
