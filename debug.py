import base64
import aiohttp
import asyncio
from solders.keypair import Keypair
from solders.keypair import Keypair as SoldersKeypair
from solders.rpc.responses import SendTransactionResp, GetSignatureStatusesResp, GetBlockHeightResp
from solders.transaction import Transaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized, Processed
from solana.rpc.types import TxOpts
from typing import Dict, Optional, Union, Any
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
import time
import logging
import requests
from colorama import Fore, init, Style
from solana.keypair import Keypair
from solana.publickey import PublicKey
import subprocess  
import sys
import json
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from solanatracker import SolanaTracker
from solders.hash import Hash
import datetime
from asyncio import Lock
from solana.rpc.async_api import AsyncClient


# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%d-%b-%y %H:%M:%S')

# Initialize colorama for colored console output
init(autoreset=True)

# Constants
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
client = AsyncClient(SOLANA_RPC_URL)
DEXSCREENER_API_URL = "https://api.dexscreener.com/latest/dex/"
TRADE_AMOUNT_IN_SOL = 0.1
CHECK_INTERVAL = 45 # Check interval in seconds
sol_usd_price = 264.0 

# Configuration for the number of tokens to invest in
MAX_TOKENS_TO_INVEST = 5  # Or any number you want

# Wallet setup
HEX_PRIVATE_KEY = "privkey hex"
PRIVATE_KEY_BYTES = bytes.fromhex(HEX_PRIVATE_KEY)
wallet_keypair = Keypair.from_secret_key(PRIVATE_KEY_BYTES)
wallet_address = str(wallet_keypair.public_key)
print(Fore.GREEN + f"Wallet Address: {wallet_address}")

# Global dictionary to track investments
investments = {}

# Dictionary to store locks for each token
token_locks = {}

# Global set to track sold tokens
sold_tokens = set()





def fetch_sol_price():
    """
    Fetches the current price of Solana (SOL) in USD from CoinGecko API.
    
    :return: float representing SOL price in USD or None if fetch fails
    """
    try:
        # Use CoinGecko's simple price endpoint
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": "solana",
            "vs_currencies": "usd"
        }
        
        response = requests.get(url, params=params, timeout=10)  # using timeout for good practice
        response.raise_for_status()  # will raise an exception for bad status codes
        
        data = response.json()
        if 'solana' in data and 'usd' in data['solana']:
            return data['solana']['usd']
        else:
            logging.error("Unexpected API response structure when fetching SOL price.")
            return None
    except requests.RequestException as e:
        logging.error(f"Failed to fetch SOL price: {e}")
        return None

# Example usage in your bot's loop or setup:
sol_usd_price = fetch_sol_price()
if sol_usd_price is None:
    # Fallback to a default or previously known value, or alert
    sol_usd_price = 264.0  # Default value
    logging.warning("Using default SOL price due to fetch failure.")
else:
    print(f"Current SOL price: ${sol_usd_price:.2f}")

# To keep the price updated, you might want to periodically call this function:
# You can adjust the interval based on how frequently you need the price to be updated
def update_sol_price_periodically(interval=3600):  # 3600 seconds = 1 hour
    global sol_usd_price
    while True:
        sol_usd_price = fetch_sol_price()
        if sol_usd_price is None:
            logging.warning("Failed to update SOL price. Using last known value.")
        else:
            print(f"Updated SOL price: ${sol_usd_price:.2f}")
        time.sleep(interval)

# Start this in a separate thread or as a task within your main coroutine if using asyncio
# asyncio.create_task(update_sol_price_periodically()) if you're in an async context

def update_sol_price():
    """
    Updates the global variable sol_usd_price with the current Solana price in USD.
    """
    global sol_usd_price
    new_price = fetch_sol_price()
    if new_price is not None:
        sol_usd_price = new_price
        logging.info(f"Updated SOL price to: ${sol_usd_price:.2f}")
    else:
        logging.warning("Failed to update SOL price.")


class SolanaTracker:
    def __init__(self, keypair: SoldersKeypair, rpc: str):
        self.base_url = "https://swap-v2.solanatracker.io"
        self.rpc = rpc
        self.keypair = keypair
        self.connection = None

    async def perform_swap(
        self,
        swap_response: Dict,
        options: Dict = {
            "send_options": {"skip_preflight": True},
            "confirmation_retries": 30,
            "confirmation_retry_timeout": 1000,
            "last_valid_block_height_buffer": 150,
            "commitment": "confirmed",
            "resend_interval": 1000,
            "confirmation_check_interval": 1000,
            "skip_confirmation_check": False,
        },
    ) -> Union[str, Exception]:
        commitment = options.get("commitment", "confirmed")
        commitment_level = self.get_commitment(commitment)

        async with AsyncClient(endpoint=self.rpc, commitment=commitment_level) as connection:
            self.connection = connection
            try:
                # Decode and prepare transaction
                serialized_transaction = base64.b64decode(swap_response["txn"])
                txn = Transaction.from_bytes(serialized_transaction)

                # Fetch the latest blockhash using curl and JSON response
                blockhash_str, last_valid_block_height = await self.get_latest_blockhash_via_curl()

                # Convert blockhash string to Hash object
                blockhash = Hash.from_string(blockhash_str)
                # Convert to SoldersKeypair
                solders_keypair = SoldersKeypair.from_bytes(self.keypair.secret_key)

                # Sign transaction
                txn.sign([solders_keypair], blockhash)

                blockhash_with_expiry = {
                    "blockhash": blockhash,
                    "last_valid_block_height": last_valid_block_height,
                }

                # Send transaction and wait for confirmation
                return await self.transaction_sender_and_confirmation_waiter(
                serialized_transaction=bytes(txn),
                blockhash_with_expiry=blockhash_with_expiry,
                options=options,
            )
            except Exception as e:
                logging.error(f"Error during swap: {e}")
                return Exception(f"Error during swap: {str(e)}")
                
    async def get_latest_blockhash_via_curl(self):
        try:
            cmd = [
                "curl",
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", '{"jsonrpc":"2.0","id":1,"method":"getLatestBlockhash"}',
                self.rpc,
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                raise Exception(f"Curl command failed: {stderr.decode()}")

            response = json.loads(stdout.decode())
            if "error" in response:
                raise Exception(f"RPC error: {response['error']}")

            blockhash = response["result"]["value"]["blockhash"]
            last_valid_block_height = response["result"]["value"]["lastValidBlockHeight"]
            return blockhash, last_valid_block_height

        except Exception as e:
            logging.error(f"Failed to fetch blockhash via curl: {str(e)}")
            raise


    async def get_swap_instructions(
        self,
        from_token: str,
        to_token: str,
        from_amount: float,
        slippage: float,
        payer: str,
        priority_fee: Optional[float] = None,
        force_legacy: bool = False,
    ) -> Dict:
        """
        Fetches swap instructions for the given parameters.

        :param from_token: Token address to swap from.
        :param to_token: Token address to swap to.
        :param from_amount: Amount of `from_token` to swap.
        :param slippage: Allowed slippage percentage.
        :param payer: Address of the payer (public key).
        :param priority_fee: Optional priority fee for transactions.
        :param force_legacy: Boolean to force legacy transaction format.
        :return: Dictionary with swap instructions.
        """
        params = {
            "from": from_token,
            "to": to_token,
            "fromAmount": str(from_amount),
            "slippage": str(slippage),
            "payer": payer,
            "forceLegacy": str(force_legacy).lower(),
        }
        if priority_fee is not None:
            params["priorityFee"] = str(priority_fee)
        url = f"{self.base_url}/swap"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    data = await response.json()
            data["forceLegacy"] = force_legacy
            return data
        except Exception as error:
            raise Exception(f"Error fetching swap instructions: {str(error)}")


    async def transaction_sender_and_confirmation_waiter(
        self,
        serialized_transaction: bytes,
        blockhash_with_expiry: Dict,
        options: Dict,
    ) -> Union[str, Exception]:
        send_options = options.get("send_options", {"skip_preflight": True})
        confirmation_retries = options.get("confirmation_retries", 30)
        confirmation_retry_timeout = options.get("confirmation_retry_timeout", 1000)
        last_valid_block_height_buffer = options.get("last_valid_block_height_buffer", 150)
        commitment = options.get("commitment", "processed")
        resend_interval = options.get("resend_interval", 1000)
        confirmation_check_interval = options.get("confirmation_check_interval", 1000)
        skip_confirmation_check = options.get("skip_confirmation_check", False)

        last_valid_block_height = blockhash_with_expiry["last_valid_block_height"] - last_valid_block_height_buffer
        retry_count = 0

        tx_opts = TxOpts(
            skip_preflight=send_options.get("skip_preflight", True),
            preflight_commitment=self.get_commitment(commitment),
            max_retries=send_options.get("max_retries", None),
        )

        response: SendTransactionResp = await self.connection.send_raw_transaction(
            serialized_transaction, tx_opts
        )

        if isinstance(response, dict):
            if 'result' in response:
                return response['result']
            
            # If the response does not contain 'result', but 'value', use that
            if 'value' in response:
                return response.get('value', {})

        if not signature:
            logging.error(f"Failed to send transaction. Response: {response}")
            return Exception("Transaction submission failed")

        if skip_confirmation_check:
            return str(signature)

        while retry_count <= confirmation_retries:
            try:
                status_response: GetSignatureStatusesResp = await self.connection.get_signature_statuses([signature])
                status = status_response.get("value", [None])[0]

                if status and self.commitment_str_to_level(str(status.get("confirmation_status", ""))) >= self.commitment_to_level(commitment):
                    return str(signature)
                if status and status.get("err"):
                    return Exception(status["err"])

                await asyncio.sleep(confirmation_check_interval / 1000)
            except Exception as error:
                if retry_count == confirmation_retries or "Transaction expired" in str(error):
                    return Exception(f"Transaction failed: {error}")
                retry_count += 1
                await asyncio.sleep(confirmation_retry_timeout / 1000)

                block_height_response: GetBlockHeightResp = await self.connection.get_block_height()
                if block_height_response.get("value", 0) > last_valid_block_height:
                    return Exception("Transaction expired")

        return Exception("Transaction failed after maximum retries")

    @staticmethod
    def get_commitment(commitment: str):
        if commitment == "confirmed":
            return "confirmed"
        elif commitment == "finalized":
            return "finalized"
        elif commitment == "processed":
            return "processed"
        else:
            raise ValueError(f"Invalid commitment: {commitment}")

    @staticmethod
    def commitment_to_level(commitment: str):
        if commitment == "confirmed":
            return 1
        elif commitment == "finalized":
            return 2
        elif commitment == "processed":
            return 0
        else:
            raise ValueError(f"Invalid commitment: {commitment}")

    @staticmethod
    def commitment_str_to_level(commitment: str):
        if commitment == "TransactionConfirmationStatus.Confirmed":
            return 1
        elif commitment == "TransactionConfirmationStatus.Finalized":
            return 2
        elif commitment == "TransactionConfirmationStatus.Processed":
            return 0
        else:
            raise ValueError(f"Invalid commitment: {commitment}")

    @staticmethod
    def get_commitment(commitment: str):
        valid_commitments = {"processed", "confirmed", "finalized"}
        if commitment in valid_commitments:
            return commitment
        raise ValueError(f"Invalid commitment: {commitment}")

    @staticmethod
    async def wait(seconds: float):
        await asyncio.sleep(seconds)

def attach_to_chrome(debugger_address="127.0.0.1:9222"):
    options = webdriver.ChromeOptions()
    options.debugger_address = debugger_address
    return webdriver.Chrome(service=Service('/opt/homebrew/bin/chromedriver'), options=options)

def fetch_new_pairs(driver):
    try:
        with tqdm(total=100, desc="Fetching pairs", unit=" token", dynamic_ncols=True) as pbar:
            driver.get("https://dexscreener.com/solana?rankBy=trendingScoreM5&order=desc")
            time.sleep(5)  # Allow page to load
            elements = driver.find_elements(By.CSS_SELECTOR, "a[href*='solana/']")
            token_addresses = [el.get_attribute("href").split("solana/")[-1] for el in elements[:100]]
            for _ in range(len(token_addresses)):
                pbar.update(1)
        logging.info(f"Fetched {len(token_addresses)} token addresses.")
        return token_addresses
    except Exception as e:
        logging.error(f"Error fetching token addresses: {e}")
        return []

def fetch_pair_data(pair_address):
    try:
        pair_data = requests.get(f"{DEXSCREENER_API_URL}pairs/solana/{pair_address}").json()
        return pair_data.get("pair", None)
    except Exception as e:
        logging.error(f"Error fetching data for pair {pair_address}: {e}")
        return None

async def fetch_current_price(token_address):
    try:
        url = f"{DEXSCREENER_API_URL}tokens/{token_address}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={}) as response:
                response.raise_for_status()
                data = await response.json()
                
                # Check if the response contains the expected structure
                if 'pairs' in data and data['pairs']:
                    pair = data['pairs'][0]  # Assuming the first pair in the list is the one we want
                    if 'priceUsd' in pair:
                        # Ensure we're converting to float
                        return float(pair['priceUsd'])  # Convert to float here
                    else:
                        logging.warning(f"Price information not found for {token_address}")
                else:
                    logging.warning(f"Unexpected response structure for {token_address}: {data}")
    
    except aiohttp.ClientError as e:
        logging.error(f"Error fetching price for {token_address} from DexScreener: {e}")
    except ValueError as e:
        logging.error(f"Error converting price to float for {token_address}: {e}")
    except Exception as e:
        logging.error(f"Unexpected error in fetch_current_price for {token_address}: {e}")
    
    return None  # or return 0 if you want to default to a numeric value

def filter_high_potential_tokens(pairs):
    high_potential = []
    for pair in pairs:
        try:
            liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
            volume_h24 = float(pair.get("volume", {}).get("h24", 0))
            price_change_h24 = float(pair.get("priceChange", {}).get("h24", 0))
            price_usd = float(pair.get("priceUsd", 0))
            
            if liquidity_usd > 1000 and volume_h24 > 5000 and price_change_h24 > 10:
                high_potential.append(pair)
        except Exception as e:
            logging.error(f"Error filtering token pair: {e}")

    # Print summary with the count of high potential tokens
    print(f"{Fore.GREEN}Selected {len(high_potential)} high potential tokens out of {len(pairs)} fetched tokens{Style.RESET_ALL}\n")
    
    return high_potential

def verify_token_info(token_address):
    update_status_bar(f"Verifying token {token_address}...")
    # Add actual verification logic here
    # For now, we'll just return True
    return True

def update_status_bar(message, status="RUNNING"):
    if status == "RUNNING":
        print(f"Status: {Fore.YELLOW if status == 'RUNNING' else Fore.GREEN if status == 'OK' else Fore.RED}{status} - {message}{Fore.RESET}")
    elif status == "OK" and "Bought token" in message:
        # Extract token address from message
        token_address = message.split("Bought token ")[1].split(".")[0]
        amount_in_sol = message.split("Amount in SOL: ")[1].split("}")[0]
        print(f"{Fore.GREEN}Total invested in: {len(investments)}{Style.RESET_ALL}")
        print(f"Token Address: {token_address}, Amount Invested: {amount_in_sol}")
    else:
        print(f"Status: {Fore.YELLOW if status == 'RUNNING' else Fore.GREEN if status == 'OK' else Fore.RED}{status} - {message}{Fore.RESET}")

async def async_buy_token(token_address: str, amount_in_sol: float, client):
    try:
        solana_tracker = SolanaTracker(keypair=wallet_keypair, rpc=SOLANA_RPC_URL)

        start_time = time.time()

        swap_response = await solana_tracker.get_swap_instructions(
            from_token="So11111111111111111111111111111111111111112",  # Wrapped SOL mint address
            to_token=token_address,
            from_amount=amount_in_sol,
            slippage=220,
            payer=str(wallet_keypair.public_key),
            priority_fee=0.0002,
            force_legacy=True
        )

        custom_options = {
            "send_options": {"skip_preflight": True, "max_retries": 5},
            "confirmation_retries": 50,
            "confirmation_retry_timeout": 1000,
            "last_valid_block_height_buffer": 200,
            "commitment": "processed",
            "resend_interval": 1500,
            "confirmation_check_interval": 100,
            "skip_confirmation_check": False,
        }

        result = await solana_tracker.perform_swap(swap_response, options=custom_options)

        if isinstance(result, str):
            transaction_id = result
            logging.info(Fore.GREEN + f"Trade successful: Transaction ID: {transaction_id}")
            logging.info(f"Transaction URL: https://solscan.io/tx/{transaction_id}")

            # Fetch current price to calculate token amount
            current_price = await fetch_current_price(token_address)
            if current_price is not None:
                # Calculate token amount received
                received_token_amount = amount_in_sol / current_price
                investments[token_address] = {
                    "price": current_price, 
                    "amount_sol": amount_in_sol,  # SOL invested
                    "token_amount": received_token_amount  # Token balance
                }
                
                update_status_bar(f"Bought token {token_address}. Amount in SOL: {amount_in_sol:.8f}", "OK")
                # Pass wallet_public_key and client to monitor_token_price
                wallet_public_key = str(wallet_keypair.public_key)
                asyncio.create_task(monitor_token_price(token_address, current_price, amount_in_sol, wallet_public_key, client))

            return transaction_id

    except Exception as e:
        logging.error(Fore.RED + f"Failed to buy token: {e}")
        return None

def buy_token(token_address: str, amount_in_sol: float) -> Optional[Dict[str, Any]]:
    return asyncio.run(async_buy_token(token_address, amount_in_sol))


async def buy_token_with_retries(token_address: str, amount_in_sol: float, retries: int = 3):
    for attempt in range(retries):
        try:
            result = await async_buy_token(token_address, amount_in_sol, client)
            if result:  # If result is a transaction ID, consider trade successful
                return result
        except Exception as e:
            if attempt < retries - 1:
                logging.warning(Fore.YELLOW + f"Transaction attempt {attempt + 1} failed for token {token_address}. Retrying. Error: {e}")
                await asyncio.sleep(1 + (attempt * 2))
            else:
                logging.error(Fore.RED + f"Failed to buy token {token_address} after multiple attempts: {e}")
                update_status_bar(f"Failed to buy token {token_address}", "ERROR")
                return None



def create_dex_swap_instruction(from_token: PublicKey, to_token: PublicKey, amount_in_sol: float, route: Any):
    """
    Create a swap instruction for a given DEX route.
    Replace this function with DEX-specific instruction generation.
    """
    # Example placeholder logic. This needs to be replaced by actual DEX swap instructions.
    pass
async def fetch_wallet_balance(client, token_address: str, wallet_public_key: str):
    try:
        opts = {"encoding": "jsonParsed"}
        response = await client.get_token_accounts_by_owner(wallet_public_key, opts=opts)
        
        if 'result' in response and response['result']['value']:
            for account_info in response['result']['value']:
                account_data = account_info['account']['data']['parsed']['info']
                # Check if 'mint' is in the correct place or if it's nested differently
                mint_address = account_data.get('mint') or account_data.get('token', {}).get('mint')
                if mint_address == token_address:
                    decimals = account_data['tokenAmount']['decimals']
                    token_balance = int(account_data['tokenAmount']['amount']) / (10 ** decimals)
                    return token_balance
            logging.error(f"No token balance found for wallet {wallet_public_key} and token {token_address}")
            return 0
        else:
            logging.error(f"No token accounts found for wallet {wallet_public_key}")
            return 0
    except Exception as e:
        logging.error(f"Error fetching wallet balance: {e}")
        return 0


async def sell_token(token_address: str, amount_in_sol: float, wallet_public_key: str, client: AsyncClient):
    """
    Sell the full amount of the token balance that was initially purchased.

    :param token_address: The SPL token address to sell.
    :param amount_in_sol: The amount of SOL that was initially invested (used for profit/loss calculation).
    :param wallet_public_key: The public key of the wallet to check the balance for.
    :param client: The Solana client instance for interacting with the blockchain.
    :return: The transaction result or None if the sale fails.
    """
    try:
        if not verify_token_info(token_address):
            logging.warning(Fore.YELLOW + f"Token {token_address} did not pass verification. Skipping sale.")
            return None

        update_status_bar(f"Selling token {token_address}...")
        logging.info(Fore.YELLOW + f"Selling token {token_address}...")

        # Fetch current price
        current_price = await fetch_current_price(token_address)
        if current_price is None:
            logging.error(f"Could not fetch current price for {token_address}, unable to calculate profit/loss.")
            return None

        if token_address in investments:
            # Fetch token balance using curl
            curl_command = [
                "curl", "-X", "POST", SOLANA_RPC_URL,
                "-H", "Content-Type: application/json",
                "-d", f'{{"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner", "params": ["{wallet_public_key}", {{"mint": "{token_address}"}}, {{"encoding": "jsonParsed"}}]}}'
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *curl_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                if process.returncode != 0:
                    raise Exception(f"Curl command failed: {stderr.decode()}")
                
                response = json.loads(stdout.decode())
                if 'error' in response:
                    raise Exception(f"RPC error: {response['error']}")

                if 'result' in response and response['result']['value']:
                    # Assuming there's only one token account for this mint
                    token_balance_info = response['result']['value'][0]['account']['data']['parsed']['info']['tokenAmount']
                    wallet_balance = float(token_balance_info['uiAmount'])
                else:
                    wallet_balance = 0
                    logging.warning(f"No token balance found for wallet {wallet_public_key} and token {token_address}")

            except Exception as e:
                logging.error(f"Failed to fetch token balance: {e}")
                return None  # Or handle this error as you see fit

            # Use the actual wallet balance for the sell amount
            sell_amount = wallet_balance

            # Calculate profit/loss
            investment_price = investments[token_address]["price"]
            profit_loss = (current_price - investment_price) * sell_amount
            logging.info(Fore.CYAN + f"Profit/Loss on sale of {token_address}: ${profit_loss:.8f}")

            # Perform the swap using the calculated token amount
            solana_tracker = SolanaTracker(keypair=wallet_keypair, rpc=SOLANA_RPC_URL)
            swap_response = await solana_tracker.get_swap_instructions(
                from_token=token_address,
                to_token="So11111111111111111111111111111111111111112",  
                from_amount=sell_amount,
                slippage=220,
                payer=str(wallet_keypair.public_key),
                priority_fee=0.0002,
                force_legacy=True
            )

            custom_options = {
                "send_options": {"skip_preflight": True, "max_retries": 5},
                "confirmation_retries": 50,
                "confirmation_retry_timeout": 1000,
                "last_valid_block_height_buffer": 200,
                "commitment": "processed",
                "resend_interval": 1500,
                "confirmation_check_interval": 100,
                "skip_confirmation_check": False,
            }

            result = await solana_tracker.perform_swap(swap_response, options=custom_options)

            if isinstance(result, str):
                transaction_id = result
                logging.info(Fore.GREEN + f"Sell transaction successful for {token_address}: {transaction_id}")
                update_status_bar(f"Sold token {token_address}", "OK")

                # Remove from investments since all tokens are sold
                del investments[token_address]
                sold_tokens.add(token_address)
                return transaction_id
            else:
                logging.error(Fore.RED + f"Failed to sell token {token_address}: {result}")
                update_status_bar(f"Failed to sell token {token_address}", "ERROR")
                return None
        else:
            logging.error(Fore.RED + f"Could not find investment for token {token_address}")
            return None

    except Exception as e:
        logging.error(Fore.RED + f"Unexpected error while selling token {token_address}: {e}")
        return None

# Adjusted fetch_token_stats to prevent duplicate stat logs
async def fetch_token_stats(token_address: str) -> Optional[Dict]:
    """
    Fetches and returns token stats asynchronously with color-coded output.
    """
    try:
        url = f"{DEXSCREENER_API_URL}tokens/{token_address}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                
                if 'pairs' in data and data['pairs']:
                    pair = data['pairs'][0]
                    current_price = float(pair.get('priceUsd', 'N/A'))

                    # Check if we've already printed stats for this token
                    if not hasattr(fetch_token_stats, f"{token_address}_printed"):
                        setattr(fetch_token_stats, f"{token_address}_printed", True)  # Mark as printed
                        print(f"{Fore.CYAN}Token Stats for {token_address}{Style.RESET_ALL}")
                        print(f"  {Fore.YELLOW}Current Price: ${current_price:.8f}{Style.RESET_ALL}")
                        print(f"  {Fore.GREEN}Liquidity: ${float(pair.get('liquidity', {}).get('usd', 0)):,}{Style.RESET_ALL}")
                        print(f"  {Fore.MAGENTA}24h Volume: ${float(pair.get('volume', {}).get('h24', 0)):,}{Style.RESET_ALL}")
                        print(f"  {Fore.BLUE}24h Price Change: {float(pair.get('priceChange', {}).get('h24', 0)):.2f}%{Style.RESET_ALL}")
                    
                    return pair
                else:
                    logging.error(f"No pair data found for {token_address}")
                    return None
    except aiohttp.ClientError as e:
        logging.error(f"Error fetching stats for {token_address}: {e}")
        return None
    except ValueError as ve:
        logging.error(f"Error converting to float for {token_address}: {ve}")
        return None



async def monitor_token_price(token_address: str, buy_price: float, amount_in_sol: float, wallet_public_key: str, client):
    profit_target = buy_price * 1.2  # 1% profit target
    stop_loss = buy_price * 0.90     # 1% stop loss
    token_name = "Unknown"  # Default name

    # Initialize lock for this token if not already done
    if token_address not in token_locks:
        token_locks[token_address] = Lock()

    try:
        # Fetch token name from the stats
        token_stats = await fetch_token_stats(token_address)
        if token_stats:
            token_name = token_stats.get('baseToken', {}).get('name', 'Unknown')
    except Exception as e:
        logging.warning(f"Failed to fetch token stats for {token_address}: {e}")

    while token_address not in sold_tokens:  # Check if the token is already sold
        try:
            # Acquire the lock to prevent concurrent actions for the same token
            async with token_locks[token_address]:
                # Check again within the lock for sold status to avoid race conditions
                if token_address in sold_tokens:
                    break

                current_price = await fetch_current_price(token_address)
                if current_price is not None:
                    if not isinstance(current_price, (int, float)):
                        try:
                            current_price = float(current_price)
                        except ValueError:
                            logging.error(f"Failed to convert current price to float for {token_address}")
                            continue

                    if current_price >= profit_target or current_price <= stop_loss:
                        # Mark as sold immediately within the lock
                        sold_tokens.add(token_address)
                        logging.info(f"Price condition hit for {token_address}. Current price: ${current_price:.8f}")
                        
                        # Execute the sell operation
                        await sell_token(token_address, amount_in_sol, wallet_public_key, client)

                        logging.info(f"Successfully sold {token_address}")
                        break  # Exit the loop once sold
                    else:
                        # Print monitoring information if not sold
                        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"Token: {token_name} - Address: {token_address}")
                        print(f"Time: {current_time} - Amount Invested: {amount_in_sol:.8f} SOL - "
                              f"Monitoring: Current price ${current_price:.8f} | Buy price ${buy_price:.8f}")
        except Exception as e:
            logging.error(f"Error in monitoring price for {token_address}: {e}")
        finally:
            if token_address not in sold_tokens:
                await asyncio.sleep(5)  # Check price every 5 seconds if not sold


def sort_pairs_by_potential(pairs):
    def potential_score(pair):
        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
        volume_h24 = float(pair.get("volume", {}).get("h24", 0))
        price_change_h24 = float(pair.get("priceChange", {}).get("h24", 0))
        return liquidity_usd * 0.3 + volume_h24 * 0.3 + price_change_h24 * 0.4
    
    return sorted(pairs, key=potential_score, reverse=True)

async def run_bot():
    driver = attach_to_chrome()
    logging.info(Fore.MAGENTA + "Starting Solana trading bot...")
    total_invested = 0
    monitoring_tasks = set()  # Using a set will automatically prevent duplicates

    while True:
        try:
            # Check if all positions are closed before fetching new pairs
            if not investments and not monitoring_tasks:  # If no investments and no monitoring tasks
                token_addresses = fetch_new_pairs(driver)
                if not token_addresses:
                    logging.warning("No token addresses fetched. Retrying in 5 minutes.")
                    await asyncio.sleep(300)  # Sleep 5 minutes before retrying
                    continue

                all_pairs = []
                with tqdm(total=len(token_addresses), desc="Fetching pair data", unit=" pair", dynamic_ncols=True) as pbar:
                    for address in token_addresses:
                        pair = fetch_pair_data(address)
                        if pair:
                            all_pairs.append(pair)
                        pbar.update(1)

                high_potential_pairs = filter_high_potential_tokens(all_pairs)
                sorted_pairs = sort_pairs_by_potential(high_potential_pairs)

                for pair in sorted_pairs[:MAX_TOKENS_TO_INVEST]:
                    token_address = pair.get("baseToken", {}).get("address", "Unknown")
                    if token_address not in investments:
                        result = await buy_token_with_retries(token_address, TRADE_AMOUNT_IN_SOL)

                        if result:
                            logging.info(Fore.GREEN + f"Invested in token: {token_address}")
                            total_invested += 1

                            # Start monitoring for the newly bought token
                            buy_price = investments[token_address]['price']
                            wallet_public_key = str(wallet_keypair.public_key)  # Ensure wallet_keypair is available
                            monitoring_task = asyncio.create_task(monitor_token_price(token_address, buy_price, TRADE_AMOUNT_IN_SOL, wallet_public_key, client))
                            
                            # Add to monitoring_tasks set only if the task isn't already present
                            monitoring_tasks.add(monitoring_task)

                update_status_bar(f"Total tokens evaluated: {len(high_potential_pairs)}, Total invested in: {total_invested}")

            else:
                # If there are active investments or ongoing monitoring, wait for them to complete
                if investments:
                    logging.info("Waiting for all investments to close before fetching new pairs.")
                await asyncio.sleep(60)  # Wait for 5 minutes to check investments' status

        except Exception as e:
            logging.error(f"Unexpected error in bot loop: {e}")
            # Wait for a bit longer before retrying in case of an error
            await asyncio.sleep(300)  # Sleep for 5 minutes before retrying

        finally:
            # Clean up any completed monitoring tasks
            # Convert set to list for asyncio.wait
            current_monitoring_tasks = list(monitoring_tasks)

            # Remove any tasks that are done
            monitoring_tasks = set(task for task in current_monitoring_tasks if not task.done())
            
            if not investments and not monitoring_tasks:  # If no active investments and no monitoring tasks
                await asyncio.sleep(300)  # Wait for 10 minutes before attempting to fetch new data

if __name__ == "__main__":
    asyncio.run(run_bot())