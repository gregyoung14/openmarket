#!/usr/bin/env python3
"""
Cancel all unfilled or partially filled orders
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from common.wallet_env import get_private_key
import json

def cancel_unfilled_orders(cancel_all=False, cancel_partial=False):
    """
    Cancel unfilled orders on Polymarket
    
    Args:
        cancel_all: If True, cancel all open orders regardless of fill status
        cancel_partial: If True, also cancel partially filled orders
    """
    # Initialize client
    private_key = get_private_key()
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=POLYGON,
        signature_type=0,  # EOA wallet mode
        funder=None
    )
    
    wallet = client.get_address()
    print(f"Wallet: {wallet}")
    print("\nDeriving API credentials...")
    
    # Set API credentials
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    
    print("\n" + "=" * 60)
    print("FETCHING ACTIVE ORDERS")
    print("=" * 60)
    
    try:
        # Get all open orders
        orders = client.get_orders()
        
        if not orders:
            print("\n✓ No active orders found")
            return
        
        print(f"\nFound {len(orders)} active order(s)")
        
        # Filter orders to cancel
        to_cancel = []
        
        for order in orders:
            order_id = order.get('id')
            status = order.get('status')
            size = float(order.get('original_size', 0))
            matched = float(order.get('size_matched', 0))
            price = order.get('price')
            side = order.get('side')
            outcome = order.get('outcome')
            
            is_unfilled = matched == 0
            is_partial = matched > 0 and matched < size
            
            print(f"\nOrder: {order_id[:16]}...")
            print(f"  Status: {status}")
            print(f"  Side: {side} {outcome}")
            print(f"  Price: ${price}")
            print(f"  Size: {size} shares")
            print(f"  Matched: {matched} shares ({(matched/size*100):.1f}% filled)")
            
            # Decide if we should cancel
            should_cancel = False
            reason = ""
            
            if cancel_all:
                should_cancel = True
                reason = "cancel_all flag set"
            elif is_unfilled:
                should_cancel = True
                reason = "completely unfilled"
            elif is_partial and cancel_partial:
                should_cancel = True
                reason = "partially filled"
            
            if should_cancel:
                print(f"  → Will cancel ({reason})")
                to_cancel.append(order_id)
            else:
                print(f"  → Keeping (partially filled, use --partial to cancel)")
        
        # Cancel orders
        if not to_cancel:
            print("\n✓ No orders to cancel")
            return
        
        print("\n" + "=" * 60)
        print(f"CANCELING {len(to_cancel)} ORDER(S)")
        print("=" * 60)
        
        for order_id in to_cancel:
            try:
                print(f"\nCanceling {order_id[:16]}...")
                response = client.cancel(order_id)
                print(f"✓ Canceled successfully")
                if response:
                    print(f"  Response: {json.dumps(response, indent=2)}")
            except Exception as e:
                print(f"✗ Failed to cancel: {e}")
        
        print("\n" + "=" * 60)
        print("DONE")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Cancel unfilled Polymarket orders')
    parser.add_argument('--all', action='store_true', 
                       help='Cancel ALL open orders (including partially filled)')
    parser.add_argument('--partial', action='store_true',
                       help='Also cancel partially filled orders (not just 100%% unfilled)')
    
    args = parser.parse_args()
    
    if args.all:
        print("⚠ WARNING: This will cancel ALL open orders")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Cancelled")
            sys.exit(0)
    
    sys.exit(cancel_unfilled_orders(cancel_all=args.all, cancel_partial=args.partial))
