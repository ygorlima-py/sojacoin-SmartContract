# Arquitetura atual do contrato

## Objetivo desta etapa

Esta etapa reorganiza o código já existente. Ela não cria unidades, permissões de funcionários, estoque por unidade nem transferências internas de soja.

`SojaCoin.sol` continua sendo o único contrato implantável. Ele reúne os módulos abaixo.

| Arquivo | Responsabilidade |
| --- | --- |
| `storage/SojaCoinStorage.sol` | Estruturas, estados, constantes e eventos compartilhados. |
| `modules/TokenModule.sol` | SJC, pausa, controle de movimentação, conversão de unidades e rotinas internas usadas por travas e resgates. |
| `modules/AdministrationModule.sol` | Armazém, auditor, participantes, auditorias e pausa administrativa. |
| `modules/DepositsModule.sol` | Depósitos, romaneios e emissão de SJC. |
| `modules/GuaranteesModule.sol` | CPRs, prioridade e reserva automática de SJC futuro. |
| `modules/LocksModule.sol` | Garantias feitas com SJC que o titular já possui. |
| `modules/ServicesModule.sol` | Serviços e tarifas cobrados pelo armazém. |
| `modules/RedemptionsModule.sol` | Solicitação, autorização, conclusão e arbitragem de retiradas. |

## Relação entre os módulos

`DepositsModule` usa `GuaranteesModule` porque, ao emitir SJC por um depósito, o contrato reserva primeiro a parte destinada às CPRs ativas. Os demais módulos usam a mesma base de token e armazenamento.

## Próxima etapa planejada

As próximas mudanças estão descritas em `implementacoes.md`: cadastro de unidades, permissões acumuláveis por carteira, estoque por unidade, retirada por unidade e transferências internas de soja. Essas regras ainda não existem no contrato atual.
