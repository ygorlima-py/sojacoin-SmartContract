# Execução — concluir o teste local de implantação dos contratos

## Objetivo desta execução

Completar o fluxo existente de implantação no Django Admin para uma rede local
Anvil. O operador envia os nove contratos, um por transação. Depois, a carteira
que é dona da SojaCoin assina as duas chamadas iniciais de configuração. O
Django consulta o RPC, grava cada contrato confirmado e só mostra a implantação
como pronta quando os dois vínculos também estiverem confirmados.

Esta execução não cria uma fábrica de implantação, não reúne os nove deploys em
uma transação e não implementa a autorização remota do dono. A carteira usada
na configuração deve ser a proprietária atual da SojaCoin. Não guardar chaves
privadas no Django.

## Regras de execução para o modelo

1. Leia este arquivo antes de cada etapa de implementação.
2. Trabalhe uma etapa por vez e marque sua caixa somente depois de editar,
   revisar e executar a verificação indicada.
3. Antes de começar a etapa seguinte, releia seu título, suas regras e seus
   critérios de aceite neste arquivo.
4. Se o código atual contrariar uma instrução, pare a mudança dependente,
   documente o conflito aqui e resolva a causa sem ampliar o escopo.
5. Preserve fluxos e dados existentes. Não apague processos, tentativas,
   transações, contratos nem histórico.
6. Use Django Templates e class-based views nos pontos web. Para as ações deste
   fluxo no Admin, use rotas próprias, POST e CSRF.
7. Toda função Python nova ou alterada deve ter argumentos e retorno tipados,
   docstring em inglês e comentários em inglês nos pontos em que expliquem uma
   decisão de segurança ou consistência.
8. Não marque um contrato como implantado apenas por ter hash. Exija recibo
   bem-sucedido, endereço criado com código na rede e vínculo local confirmado.
9. Não chame RPC para enviar transações. A carteira do navegador envia; Django
   prepara, verifica, registra e consulta.
10. Em cada etapa, escreva abaixo os arquivos tocados e o resultado da
    verificação. Não declare a etapa concluída se um item do aceite falhou.

## Estado inicial verificado

- [x] Criado este arquivo antes das alterações de implementação. Ele será
  consultado e atualizado etapa por etapa até o encerramento.
- [x] O plano principal descreve nove etapas individuais e MetaMask no Admin.
- [x] `DeploymentAttempt` guarda dados preparados e a tentativa de envio.
- [x] `confirm_transaction_from_chain` já lê recibos de transação enviados.
- [x] `register_contract` já grava um contrato com validação local.
- [x] `SojaCoin.configureUnitRegistry` e
  `SojaCoin.configureIssuanceBatches` chamam funções `onlyOwner`.
- [x] Serviço de confirmação do recibo cria e vincula o `ContractInstance` à
  etapa somente depois de validar a transação e o endereço no RPC (Etapa 1).
- [x] O Admin oferece consulta POST protegida do recibo para liberar a etapa
  seguinte (Etapa 2).
- [x] As duas configurações finais têm tentativa persistida e confirmação pelo
  RPC, usando serviços compartilháveis com um futuro worker (Etapa 5).

## Etapa 1 — Confirmar deploy individual e salvar o contrato

### Instruções

1. Releia `services/deployment.py`, `services/transaction_confirmation.py`,
   `services/transactions.py`, `services/contract_registration.py` e os métodos
   `DeploymentStep.clean`, `DeploymentStep.is_confirmed` e
   `DeploymentProcess.clean`.
2. Crie em `services/deployment_confirmation.py` um serviço público que receba
   `attempt_id: int` e retorne um resultado tipado que diferencie pendente,
   falha e confirmação com o `ContractInstance`.
3. Busque a tentativa por ID junto da etapa, processo, transação, empresa e
   rede. Confirme que todos pertencem ao mesmo processo, empresa e rede e que o
   processo segue em andamento.
4. Para transação `SUBMITTED`, use o RPC da rede salva. Confira a transação e o
   recibo com a mesma hash, remetente gravado, destino vazio e `input` igual a
   `DeploymentAttempt.prepared_data`.
5. Se o RPC ainda não tiver recibo, retorne pendente sem alterar estado algum.
6. Se o recibo tiver `status == 0`, use `fail_transaction` e libere a tentativa
   conforme a regra existente. Preserve a tentativa e a hash no histórico.
7. Para recibo bem-sucedido, exija hash, bloco, `contractAddress` válido e
   bytecode no endereço consultado no bloco do recibo.
8. Descubra a versão a partir dos artefatos empacotados usados no preparo.
   Registre essa versão sem inventar um valor genérico diferente do padrão
   existente.
9. Em uma transação do banco, confirme `BlockchainTransaction`, crie ou
   recupere idempotentemente `ContractInstance`, vincule o contrato ao
   `DeploymentStep` e feche a tentativa aberta. Faça a ordem respeitar a
   validação atual de `DeploymentStep.clean`.
10. Se uma consulta repetida encontrar a tentativa confirmada e o mesmo
    contrato já vinculado, devolva o registro existente. Se os registros
    divergirem, gere erro para revisão sem substituir endereço ou histórico.
11. Não faça chamada externa ao RPC enquanto mantém locks do banco. Só abra a
    transação de banco para a aplicação atômica do resultado já verificado.

### Aceite e verificação

- [x] Recibo pendente não altera dados.
- [x] Recibo revertido marca falha sem criar contrato.
- [x] Recibo bem-sucedido grava um contrato com hash e vincula a etapa.
- [x] Remetente, destino, input, chain id e código incorretos são rejeitados.
- [x] Consulta repetida não cria duplicatas.
- [x] Falha na gravação reverte a confirmação local.
- [x] `./.venv/bin/python manage.py test blockchain.tests.DeploymentReceiptConfirmationTests`
  passou com 9 testes, incluindo a sequência de nove deploys.

Resultado desta etapa:

- Arquivos alterados: `services/deployment_confirmation.py`, `models.py`,
  `migrations/0007_alter_contractinstance_version.py` e `tests.py`.
- Verificação: 9 testes passaram. A versão do contrato guarda `sha256:` mais o
  hash completo de 64 caracteres do artefato usado. A gravação do recibo, do
  contrato e do vínculo com a etapa ocorre em uma única transação do banco.

## Etapa 2 — Consultar a confirmação no Django Admin

### Instruções

1. Releia esta etapa antes de alterar `admin.py` ou templates.
2. Adicione rota Admin própria de consulta, com `admin_site.admin_view`, POST e
   CSRF. Use a mesma permissão de operação de implantação já aplicada às ações
   MetaMask.
3. Carregue processo, etapa e tentativa pela URL em conjunto. Se os IDs não
   corresponderem entre si, responda 404.
4. Chame o serviço da Etapa 1. A rota não deve preparar nem enviar transação.
5. Na linha da etapa `SUBMITTED`, mostre **Consultar confirmação**. Ao confirmar,
   exiba o endereço do contrato e atualize a página para mostrar a próxima
   etapa liberada.
6. Mostre mensagens curtas para pendente, falha, confirmação e indisponibilidade
   do RPC. Erro de RPC deve preservar estado e permitir nova consulta da mesma
   tentativa.
7. Mantenha os estados `Preparada`, `Aguardando confirmação`, `Falhou`,
   `Confirmada` e `Precisa de revisão` coerentes com banco e cadeia.

### Aceite e verificação

- [x] GET na rota não consulta nem altera transação; a rota aceita somente POST.
- [x] POST sem permissão, CSRF ou escopo correspondente é recusado.
- [x] Pendente e erro de RPC preservam a tentativa para nova consulta, sem
  preparar nem enviar outra transação.
- [x] Confirmação retorna o endereço e a atualização da tela libera a etapa
  seguinte na ordem existente.
- [x] `./.venv/bin/python manage.py test blockchain` passa para esta etapa.

Resultado desta etapa:

- Arquivos alterados: `admin.py`, `static/blockchain/deployment.js` e `tests.py`.
- Verificação focada: 13 testes de confirmação e Admin passaram. Verificação
  final: a suíte completa passou com 116 testes na Etapa 7.

## Etapa 3 — Fechar corretamente os nove deploys

### Instruções

1. Releia `DEPLOYMENT_ORDER`, `_previous_steps_confirmed`,
   `prepare_deployment_step` e a validação de status no model.
2. Confirme que o serviço da Etapa 1 salva os nove `ContractInstance` na empresa
   e rede do processo e que cada hash liga à tentativa correta.
3. Confirme que os seis módulos precedem SojaCoin e que os endereços dos seis
   módulos vêm das etapas deste mesmo processo.
4. Confirme que Unit Registry e Issuance Batches recebem o endereço de SojaCoin
   confirmada deste mesmo processo.
5. Após a nona etapa ser confirmada, marque o processo como
   `DeploymentProcess.Status.DEPLOYED` usando a regra já existente. Não marque
   `DEPLOYED` se faltar etapa, contrato, recibo confirmado ou hash correspondente.
6. Não marque ainda o sistema como operacional. As duas configurações da
   próxima etapa ainda estarão pendentes.

### Aceite e verificação

- [x] Nove etapas confirmadas produzem nove contratos distintos ligados à
  mesma empresa, rede e processo.
- [x] O processo não conclui com oito contratos, contrato sem hash, tentativa
  falha ou confirmação reorged.
- [x] Repetir a confirmação final não duplica contrato nem altera hash.
- [x] Teste da sequência completa com RPC simulado passa.

Resultado desta etapa:

- Arquivos alterados: `services/deployment_confirmation.py`, `tests.py` e este
  arquivo de execução.
- Verificação: 9 testes do serviço de confirmação passaram, entre eles o
  fluxo que cria e confirma cada etapa em ordem e só muda o processo para
  `Contratos implantados` após a nona. A proteção contra conclusão incompleta
  já é aplicada por `DeploymentProcess.clean` e possui teste de regressão.

## Etapa 4 — Persistir tentativas das duas configurações finais

### Instruções

1. Releia o model e serviço de transações antes de escolher onde guardar estado.
2. Adicione um model simples para representar cada configuração do processo:
   `unit_registry` ou `issuance_batches`, com estado, hash, remetente, horário,
   dados preparados e relação com `BlockchainTransaction`. Uma tentativa deve
   ter empresa/rede/proc sem ambiguidade, usando o processo como escopo.
3. Impeça mais de uma tentativa aberta para o mesmo processo e tipo de
   configuração com restrição no banco, além das validações do serviço.
4. Preserve cada tentativa encerrada. Não permita editar destino, tipo ou dados
   preparados depois da criação.
5. Crie migration e atualize Admin de leitura para expor estado e hash sem
   permitir edição manual.
6. Rode `makemigrations --check --dry-run` e migrações de teste.

### Aceite e verificação

- [x] Há no máximo uma configuração aberta de cada tipo por processo.
- [x] Tentativas encerradas continuam consultáveis; estado, remetente, hash e
  horários vêm do registro de transação relacionado.
- [x] Empresa e rede pertencem ao processo salvo; o novo registro não recebe
  esses valores de dados do navegador.
- [x] A migração aplica na base de teste e `makemigrations --check --dry-run`
  não detecta mudanças pendentes.

Resultado desta etapa:

- Arquivos alterados: `models.py`, `migrations/0008_deploymentconfigurationattempt.py`,
  `admin.py`, `tests.py` e este arquivo.
- Verificação: 4 testes do model passaram; `manage.py check` e
  `makemigrations --check --dry-run` passaram. O registro preserva a chamada
  preparada e seu endereço esperado; a transação relacionada fornece estado,
  hash, remetente e datas sem duplicar esses dados.

## Etapa 5 — Enviar e confirmar as duas configurações

### Instruções

1. Releia os dois métodos Solidity e a ABI de SojaCoin. Confirme os nomes e
   assinaturas exatos antes de codificar qualquer chamada.
2. Permita iniciar configuração apenas quando os nove contratos estiverem
   confirmados e o processo estiver `DEPLOYED`.
3. Use `ContractInstance` e `DeploymentStep` do mesmo processo. O `to` da
   transação é o endereço de SojaCoin. O `data` deve ser codificado no servidor
   com o endereço do contrato relacionado.
4. Consulte `owner()` pelo RPC. Exija que a conta MetaMask e o remetente sejam
   exatamente o dono atual. Confira também o `chain_id` esperado.
5. Para Unit Registry, espere `configureUnitRegistry(registry_address)`. Para
   Issuance Batches, espere `configureIssuanceBatches(batches_address)`.
6. Antes do popup da MetaMask, salve a tentativa e o marcador de envio iniciado.
   Ao receber a hash, consulte a transação no RPC e valide remetente, destino,
   valor zero e dados preparados antes de salvar `SUBMITTED`.
7. Trate rejeição explícita como falha recuperável. Erro ambíguo permanece
   aberto, bloqueia nova tentativa e oferece conferência pela mesma hash.
8. Adicione serviço de confirmação por ID da tentativa. Confira recibo e evento
   correspondente emitido pela SojaCoin. Depois leia o endereço configurado no
   RPC e compare com o endereço esperado antes de concluir.
9. Faça a confirmação da transação e estado da configuração na mesma transação
   do banco. A repetição deve retornar o resultado existente.
10. Atualize status derivado do processo para mostrar separadamente
    **Nove contratos implantados** e **Configuração concluída**. Só esta última
    permite a tela informar que as ligações estão prontas.

### Aceite e verificação

- [x] Carteira diferente do dono, rede errada e processo fora do estado exigido
  são bloqueados no servidor.
- [x] Cada tipo tem destino, método e codificação ABI corretos.
- [x] Rejeição, pendência, RPC fora e recuperação de hash preservam histórico.
- [x] Recibo bem-sucedido com evento ou vínculo divergente vai para revisão.
- [x] Os testes confirmam as duas chamadas e comparam os endereços retornados
  pelos getters da SojaCoin com os esperados.

Resultado desta etapa:

- Arquivos alterados: `services/deployment_configuration.py`,
  `services/transactions.py`, `models.py`, `migrations/0008_deploymentconfigurationattempt.py`,
  `admin.py`, `static/blockchain/deployment.js`, `tests.py`.
- Verificação: 30 testes focados de serviço e Admin passaram. As duas chamadas
  usam ABI real empacotada; remetente, destino, rede, valor, dados, recibo,
  evento e getter são verificados. A validação em uma rede Anvil real está
  registrada na Etapa 7, pois o ambiente bloqueou abrir a porta RPC local.

## Etapa 6 — Interface, permissões e clareza do fluxo

### Instruções

1. Releia o template do processo e os estilos existentes antes de editar.
2. Na página Admin, mantenha as nove linhas de deploy e mostre configuração como
   uma seção posterior, com uma linha por vínculo.
3. Desabilite ações de configuração até nove contratos confirmados.
4. Mostre nome do vínculo, estado, hash e endereço esperado de forma legível.
5. Use somente botões POST protegidos com CSRF. Todas as regras de empresa,
   rede, estado, remetente e tentativa precisam ser revalidadas no servidor.
6. Não adicione campos editáveis para `company`, `network`, `owner`, `to`,
   `operation_type` ou `transaction_data` no formulário do navegador.

### Aceite e verificação

- [x] O operador distingue implantação dos contratos e configuração dos vínculos.
- [x] A interface não oferece ação fora de ordem ou para processo cancelado/em
  revisão.
- [x] As rotas recusam GET, usuário sem permissão e tentativa fora do processo.

Resultado desta etapa:

- Arquivos alterados: `admin.py`,
  `templates/admin/blockchain/deploymentprocess/change_form.html`,
  `static/blockchain/deployment.css`, `static/blockchain/deployment.js` e
  `tests.py`.
- Verificação: os testes focados confirmam que a seção só aparece depois dos
  nove deploys (ou para exibir histórico de configuração em revisão), que o
  resumo mostra a contagem real de contratos confirmados e que os botões usam
  POST com CSRF e permissão do Admin.

## Etapa 7 — Testes de regressão e teste integrado local

### Instruções

1. Releia todos os critérios das etapas 1 a 6 antes de completar os testes.
2. Adicione testes de serviço para recibos pendentes, sucesso, falha, RPC
   divergente, código ausente, dependência inválida, repetição e rollback.
3. Adicione testes Admin para autenticação/permissão, POST, CSRF, escopo de URL
   e mensagens de estado.
4. Adicione testes para as duas configurações, dono correto/incorreto, chamada
   divergente, tentativa duplicada, confirmação idempotente e vínculo divergente.
5. Execute `./.venv/bin/python manage.py test blockchain` e corrija todas as
   falhas deste escopo.
6. Execute `./.venv/bin/python manage.py check` e
   `./.venv/bin/python manage.py makemigrations --check --dry-run`.
7. Se Anvil estiver disponível, execute uma implantação real local usando
   contas de teste. Não use uma carteira com fundos reais.
8. Para cada envio, confira hash e recibo no RPC. Ao final confira os nove
   endereços, owner da SojaCoin e Unit Registry e os dois vínculos.
9. Se MetaMask/browser não estiver disponível, não declare o teste ponta a
   ponta concluído. Registre claramente o que os testes de serviço provaram e
   o que ainda depende da interação manual.

### Aceite e verificação

- [x] Todos os testes Django do app passam.
- [x] Checks e migrations estão limpos.
- [x] A limitação do teste real com Anvil está descrita.
- [x] Nenhum deploy foi enviado a uma rede pública.

Resultado desta etapa:

- Arquivos alterados: `tests.py`, `services/deployment_configuration.py`,
  `admin.py`, arquivos da tela de implantação e este checklist.
- Verificação automatizada: `./.venv/bin/python manage.py test blockchain`
  passou com 116 testes; `manage.py check` não encontrou problemas;
  `makemigrations --check --dry-run` não encontrou alterações pendentes; e
  `node --check blockchain/static/blockchain/deployment.js` passou.
- Verificação integrada: Anvil está instalado, mas não iniciou. O ambiente
  negou a abertura de `127.0.0.1:8545` com `Operation not permitted`. Não havia
  navegador MetaMask disponível. Portanto, não foram coletadas hashes nem
  endereços de uma rede real local; o teste com a carteira segue pendente de
  execução manual em uma máquina que permita iniciar Anvil e abrir MetaMask.
- Segurança do teste: nenhum deploy foi enviado a uma rede pública e os testes
  Django usaram banco temporário.

## Etapa 8 — Documentação final e encerramento

### Instruções

1. Releia este arquivo inteiro e confira cada critério marcado.
2. Atualize `app/blockchain/PLAN.md` para separar o que já foi implementado do
   que continua pendente.
3. Atualize `app/blockchain/README.md` com como iniciar e conferir o fluxo local,
   as nove confirmações de implantação e as duas configurações.
4. Mantenha este `planejamento.md` com caixas concluídas e evidência curta em
   cada etapa.
5. Informe os arquivos alterados, como iniciar o fluxo no Admin, número de
   confirmações, testes executados e resultado real do Anvil.

### Aceite e verificação

- [x] Plano e README correspondem ao comportamento executável.
- [x] Cada etapa deste arquivo tem resultado registrado.
- [x] O relatório final distingue teste automatizado de interação MetaMask real.

Resultado desta etapa:

- Arquivos alterados: `app/blockchain/PLAN.md`, `app/blockchain/README.md` e
  este arquivo.
- Verificação: documentos revisados junto ao resultado dos 116 testes e à
  limitação observada ao iniciar Anvil.
