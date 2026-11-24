### Pré-requisito — Implantação inicial dos nove contratos

Antes de registrar unidades ou emitir tokens, cada empresa precisa ter os nove
contratos implantados e os dois vínculos da SojaCoin configurados na rede
selecionada.

#### Fluxo implementado

- No Django Admin, abrir **Blockchain → Processos de implantação** e clicar em
  **Iniciar implantação**. Escolher empresa ativa, rede ativa e informar o
  endereço público que será dono da SojaCoin e do Unit Registry.
- O sistema cria nove etapas na ordem correta: seis módulos, SojaCoin, Unit
  Registry e Issuance Batches. O processo bloqueia quando já há contrato ativo
  de um desses tipos para a mesma empresa e rede.
- O operador envia cada contrato pela MetaMask, em uma transação própria. A
  carteira assina nove vezes durante os deploys. O Django registra a tentativa
  antes de abrir a carteira e confere remetente, rede e dados de criação no RPC.
- A ação **Consultar confirmação** consulta somente a transação salva; não abre
  a carteira nem envia outra. O endereço só é gravado depois do recibo bem
  sucedido e da confirmação de que há código no endereço criado. Cada gravação
  de contrato, confirmação e vínculo à etapa ocorre de forma atômica no banco.
- O processo só muda para **Contratos implantados** depois das nove etapas
  confirmadas. As etapas seguintes usam endereços confirmados do mesmo processo.
- Depois dos nove deploys, a seção **Configuração dos vínculos** oferece duas
  ações separadas: ligar o Unit Registry e ligar Issuance Batches à SojaCoin.
  Cada ação exige que a MetaMask esteja na rede certa e conectada à carteira que
  é dona atual da SojaCoin. São mais duas transações e duas confirmações da
  carteira: onze no total para completar o fluxo.
- O Django gera os dados das chamadas com a ABI e verifica remetente, destino,
  valor zero e dados no RPC. Na confirmação, exige recibo bem sucedido, evento
  esperado e o endereço correto lido da SojaCoin no bloco da transação.
- A tela distingue **Nove contratos implantados** de **Configuração concluída**.
  Só mostra os vínculos como concluídos quando as duas chamadas foram
  confirmadas. Divergência ou reorganização mantém o histórico e coloca o
  processo em revisão.
- Rejeição explícita da carteira ou falha confirmada libera nova tentativa sem
  apagar registros. Se o navegador ou RPC deixar o envio incerto, a tentativa
  permanece aberta e a mesma hash pode ser conferida de novo. O Django nunca
  recebe nem armazena a chave privada.

#### Fora do fluxo local implementado

- A MetaMask não foi aberta neste ambiente. A confirmação visual das onze
  solicitações de assinatura ainda depende de teste manual no navegador.
- A inicialização de Anvil foi tentada, mas o ambiente de execução bloqueou a
  abertura da porta local com `Operation not permitted`. Portanto, não houve
  deploy real nesta execução; os testes Django usam RPC simulado.
- A detecção automática de reorganizações por consulta periódica, worker ou
  Celery continua pendente. O serviço que marca uma transação reorganizada e
  coloca o processo em revisão já existe.
- O registro de unidades na blockchain e as tarefas Celery de consulta também
  são etapas futuras independentes.

Um contrato só é considerado implantado após recibo bem sucedido, código no
endereço criado e vínculo local confirmado. Uma hash recebida pela carteira,
sozinha, nunca conclui uma etapa.

### 1. Django conecta ao RPC

O backend Django precisa de uma biblioteca Python para falar com uma blockchain EVM, como web3.py.

No ambiente local, o registro `Network` no banco guarda:

- `rpc_url`: `http://127.0.0.1:8545`
- `chain_id`: `31337`

Cada rede pode apontar para seu próprio RPC. O serviço compara o `chain_id`
respondido pelo RPC com o valor do mesmo registro selecionado no processo.

Então o Django cria uma conexão assim, internamente:

from blockchain.services.rpc import connect_to_rpc

web3 = connect_to_rpc(network)

Essa conexão permite ao Django:

- verificar se a rede está disponível;
- conferir o chainId;
- consultar contratos;
- consultar saldo, unidades e permissões;
- buscar uma transação pelo hash;
- obter o recibo da transação;
- ler eventos como UnitRegistered;
- confirmar se uma transação deu certo ou falhou.

O Django usará essa conexão para leitura e acompanhamento. Ele não terá a chave privada do usuário.

### 2. O navegador conecta à MetaMask

Na implantação, o JavaScript pede à MetaMask a carteira pública do operador e
confere se ela está na rede do processo. O JavaScript recebe do Django os dados
de criação preparados para aquela etapa e pede à MetaMask para enviá-los. A
carteira informada no início do processo continua sendo usada como dona dos
contratos que exigem um endereço de dono.

A MetaMask mantém a chave privada e pede a aprovação do operador. O Django recebe
o endereço público e, depois do envio, a hash pública da transação.

### 3. A intermediação entre os dois

1. O operador clica na ação da etapa liberada no detalhe do processo do Admin.
2. Django valida o processo e grava uma tentativa `PREPARED` com a empresa, a
   rede, a carteira remetente e os dados exatos de criação.
3. Antes de abrir a confirmação da MetaMask, Django grava que o pedido de envio
   começou. Isso impede um segundo envio se o navegador perder a resposta.
4. MetaMask pede a confirmação e envia a transação de criação.
5. O navegador mostra a hash e a envia ao Django. Se o RPC ainda não a encontrar
   ou o servidor falhar, a mesma hash pode ser enviada novamente para a mesma
   tentativa.
6. Django consulta o RPC da rede salva e compara carteira remetente, rede,
   destino vazio (criação de contrato) e dados de criação.
7. Se a transação corresponder ao que foi preparado, Django registra a hash como
   `SUBMITTED` e mostra **Aguardando confirmação**.
8. O botão **Consultar confirmação** pede ao Django para verificar recibo,
   transação e bytecode no RPC. O Django grava o endereço e libera a etapa
   seguinte somente após a confirmação.
9. Depois de confirmar as nove etapas, o dono atual da SojaCoin envia duas
   chamadas separadas para ligar Unit Registry e Issuance Batches. O Django
   confirma recibo, evento e endereço lido do contrato.

### 4. O que precisa ser construído no SaaS

- [x] Instalar web3.py e configurar o RPC URL e chain ID em cada registro
  `Network` do banco.
- [x] Criar `blockchain/services/rpc.py` para conectar ao RPC e conferir a rede.
- [x] Empacotar as ABIs atuais e criar `blockchain/services/contracts.py` para
  ligar uma instância salva ao endereço e à ABI corretos.
- [x] Criar `blockchain/services/transaction_confirmation.py` para consultar o
  recibo, decodificar os eventos e registrar o resultado usando os serviços de
  transação e evento já existentes.
- [x] Criar no Django Admin a tela para iniciar e acompanhar o processo local
  dos nove contratos, sem enviar transações.
- [x] Conectar MetaMask ao processo de implantação, registrar tentativas e
  verificar a transação no RPC antes de marcar seu envio.
- [x] Consultar recibos de implantação e gravar cada endereço somente depois
  do sucesso confirmado no RPC.
- [x] Liberar a ação seguinte somente depois que a etapa anterior estiver
  ligada a um contrato com transação confirmada.
- [x] Configurar separadamente Unit Registry e Issuance Batches na SojaCoin,
  verificando dono, rede, transação, recibo, evento e endereço final.
- [x] Mostrar separadamente os nove deploys e os dois vínculos, sem declarar a
  configuração concluída antes das duas confirmações.
- [x] Criar o handler do evento `UnitRegistered` e o comando manual
  `companies/management/commands/reconcile_unit_registrations.py`. O comando
  chama o serviço de confirmação sem depender de Celery.
- [ ] Criar futuramente uma tarefa de acompanhamento em segundo plano que use o
  serviço de confirmação já existente.
- [ ] Criar a ação de registro da unidade e mostrar os estados preparada, enviada,
  confirmada ou falhou.

A consulta ao RPC usa o endereço salvo na rede escolhida e exige que esse RPC
esteja em execução. O fluxo local
implementado pode preparar, enviar pela MetaMask, consultar e registrar os nove
deploys e as duas configurações. Nesta execução, os testes automatizados
passaram com RPC simulado. Anvil não pôde ser iniciado porque o ambiente negou a
abertura da porta local, e não havia navegador MetaMask disponível para teste
manual.

A regra é:

- MetaMask envia transações que pertencem ao usuário.
- Django consulta, acompanha, registra e confirma o resultado.
- Django só enviaria uma transação se existisse uma carteira institucional controlada pelo sistema, o que não será usado neste fluxo inicial.
