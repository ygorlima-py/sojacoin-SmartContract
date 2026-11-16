// SPDX-License-Identifier: MIT
// Compatible with OpenZeppelin Contracts ^5.7.0
pragma solidity ^0.8.27;

import {AdministrationModule} from "./modules/AdministrationModule.sol";
import {DepositsModule} from "./modules/DepositsModule.sol";
import {LocksModule} from "./modules/LocksModule.sol";
import {RedemptionsModule} from "./modules/RedemptionsModule.sol";
import {ServicesModule} from "./modules/ServicesModule.sol";
import {TokenModule} from "./modules/TokenModule.sol";
import {SojaCoinStorage} from "./storage/SojaCoinStorage.sol";

// Contrato que reúne os módulos da primeira versão da SojaCoin.
// Esta composição não cria unidades, novas permissões ou transferências internas;
// ela apenas separa o código existente por responsabilidade.
contract SojaCoin is
    AdministrationModule,
    DepositsModule,
    ServicesModule,
    LocksModule,
    RedemptionsModule
{
    /// @notice Cria o contrato para uma empresa, definindo o dono e o armazém iniciais.
    /// @dev O endereço do armazém precisa ser diferente de zero.
    /// @param initialOwner Carteira que administrará o contrato.
    /// @param _wareHouse Carteira operacional inicial do armazém.
    constructor(address initialOwner, address _wareHouse)
        TokenModule(initialOwner)
    {
        require(_wareHouse != address(0), "Armazem invalido");
        wareHouse = _wareHouse;
    }
}
