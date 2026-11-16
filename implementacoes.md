# Implementações futuras — SojaCoin

## Objetivo

Refatorar o sistema para atender uma cooperativa ou revenda com vários armazéns, mantendo a primeira versão dentro de uma única empresa e permitindo expansão futura para outras empresas.

## Status

- Concluído: separação do contrato atual em módulos, sem mudar as regras de negócio existentes.
- Pendente: todas as funcionalidades listadas no escopo da primeira etapa abaixo.

## Escopo da primeira etapa

1. **Unidades da empresa:** uma implantação representa uma empresa; cada armazém/unidade tem um identificador próprio e estoque físico separado.
2. **Permissões de funcionários:** uma carteira pode acumular várias permissões, gerais ou limitadas a uma unidade. Não exigir uma carteira diferente para cada função.
3. **Transferência interna de soja:** registrar unidade de origem, destino, quantidade, responsáveis e estados da transferência (solicitada, em transporte, recebida ou cancelada). A transferência muda o estoque físico entre unidades; não cria, queima nem transfere SJC.
4. **Depósito e retirada por unidade:** registrar em qual unidade a soja entrou e de qual unidade será retirada. Uma retirada só pode ser atendida se a unidade escolhida tiver estoque disponível.
5. **SJC e garantias:** manter o SJC fungível dentro da empresa. CPRs e outras travas continuam vinculadas ao produtor e aos documentos da operação, não a uma unidade específica.
6. **Rastreabilidade:** emitir eventos para mudanças de unidade, estoque, permissões e responsáveis, mantendo referências aos documentos por hash.

## Preparação para expansão

Não implementar agora transferências, saldos ou liquidação entre empresas. Manter os módulos e identificadores organizados para que uma etapa futura possa adicionar empresas e integração entre elas sem misturar essa lógica ao estoque e às operações internas.

## Diretrizes para a refatoração

- Separar responsabilidades em módulos de unidades, permissões, estoque, transferências, depósitos/retiradas, SJC e garantias.
- Preservar as regras já acordadas para emissão, CPR, tarifas e resgates, salvo decisão explícita para alterá-las.
- Não introduzir proxy atualizável nesta etapa; mudanças futuras de contrato devem prever nova versão e migração dos dados/saldos.
- Antes de implementar, conferir o contrato atual e propor mudanças compatíveis com este documento; não presumir regras comerciais que não estejam especificadas aqui.
