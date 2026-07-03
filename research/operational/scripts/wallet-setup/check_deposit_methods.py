#!/usr/bin/env python3
"""
Check ClobClient for deposit methods
"""
from py_clob_client.client import ClobClient
import inspect

# Check for deposit-related methods
client_methods = [m for m in dir(ClobClient) if 'deposit' in m.lower() or 'collateral' in m.lower() or 'allowance' in m.lower()]
print("Deposit/Collateral related methods:")
for method in client_methods:
    print(f"  - {method}")
    
# Check for update_balance_allowance
if 'update_balance_allowance' in dir(ClobClient):
    print("\n\nupdate_balance_allowance signature:")
    print(inspect.signature(ClobClient.update_balance_allowance))
    print("\nDocstring:")
    print(ClobClient.update_balance_allowance.__doc__)
