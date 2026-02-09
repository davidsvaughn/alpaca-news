import os
import schwabdev  # import the package
from dotenv import load_dotenv

load_dotenv()
SCHWAB_APP_KEY = os.getenv("SCHWAB_APP_KEY")
SCHWAB_APP_SECRET = os.getenv("SCHWAB_APP_SECRET")

client = schwabdev.Client(SCHWAB_APP_KEY, SCHWAB_APP_SECRET)  # create a client

print(client.quotes("NVDA").json())  # make api calls