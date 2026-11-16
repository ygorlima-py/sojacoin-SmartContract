# SojaCoin

Contrato para registrar depósitos de soja e emitir SJC lastreado no estoque informado. **1 SJC representa 1 saca de 60 kg**; o token usa seis casas decimais para permitir frações de saca.

## O que o contrato faz hoje

- Registra participantes, depósitos, romaneios e emissão de SJC.
- Permite transferir SJC entre participantes cadastrados.
- Registra CPRs com reserva prioritária de SJC emitido em depósitos futuros e permite travar SJC já existente como garantia.
- Registra serviços e tarifas ligados a depósitos e permite solicitar retiradas de soja.
- Guarda hashes de documentos; os arquivos originais ficam fora da blockchain.

O contrato atual representa uma empresa com uma carteira de armazém e uma de auditor. Cadastro de várias unidades, estoque separado por unidade e transferências físicas entre unidades ainda estão planejados, mas não implementados. Veja [implementacoes.md](implementacoes.md).

## Estrutura do contrato

`SojaCoin.sol` é o contrato implantável e reúne módulos para token, administração, depósitos, CPRs, travas, tarifas e retiradas. A descrição está em [docs/arquitetura.md](docs/arquitetura.md).

## Testar em rede local

Inicie o Anvil no terminal:

```bash
~/.foundry/bin/anvil --host 127.0.0.1 --port 8545 --gas-price 0 --block-base-fee-per-gas 0
```

Na extensão Solidity (Wake) do VS Code, conecte a uma rede existente com:

```text
RPC: http://127.0.0.1:8545
Chain ID: 31337
```

Mantenha o Anvil aberto enquanto testa. As contas e chaves exibidas por ele são públicas e servem somente para a rede local; não use essas chaves com ativos reais.
