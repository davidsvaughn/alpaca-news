The script 'alpaca/news_websocket.py' is running continuously and saving each incoming financial news item to the 'output/alpaca' folder (each item is a separate json file).

I have also installed 'schwabdev' library  ('https://github.com/tylerebowers/schwabdev') and have signed up for the proper schwab developer account, so I can access real time and historical financial data (see 'schwab/main.py' for a simple example).
For full documentation and examples on 'schwabdev' please use the context7 mcp server.

I want to design and implement an LLM-based day-trading stock research assistant that attempts to identify current, real-time trends in stock price movements that are likely to continue in the short term, that could represent realizable opportunity for profit.

The system should be triggered by each news item as it comes in to the 'output/alpaca' folder, and then:

- first evaluates whether this is a "fresh" news item that deserves further scrutiny because it could be a signal of imminent market movement, or whether this is just a "fluff" piece that should be ignored (e.g. "if you had invested in this stock 2 years ago, this is what you would have now..."). Making this determination could involve using OpenAI, Google Gemini, or Grok models, together with their respective web search tools, to find further information related to the stock(s) in question, and the news content of the Alpaca news item.
- if this news is worth further scrutiny, determine which stock, or stocks, should be investigated further
- use 'schwabdev' to gather recent historical financial data, and possibly initiate 'streams' to start monitoring current data in real-time (see schwabdev docs / context7 - I read you should not keep hitting REST API endpoints for current data... you should use streams)
- use OpenAI, Google Gemini, or Grok models, together with their respective web search tools, to find any evidence of real-time sentiment, real-time trends that are currently unfolding, within a very immediate time window (last hour, last 12 hours, etc). Here it is important to evaluate web search sources, and possibly cross-reference them, to establish how current the information is.
- use Grok's 'x_search' tool to query whether there are any recent or trending developments related to the stock(s) in question that are discernible from X (twitter) posts. Use context7 mcp serever for help with this too.

If a possible opportunity is found (like 'buy this stock now!') then the system would need to monitor the stock price, and any evolving trends, to be able to decide when to sell the stock, to maximize profit.

A very important aspect of this system should be that it is able to learn and evolve from experience.
A system needs to be devised for storing what is being 'learned' so this knowledge can be effectively used in future iterations of the system, allowing it to evolve and improve.
Some examples of ways I'd like the system to learn are: 
- noticing certain clues (keywords, phrases, etc) that indicate an alpaca news item is not worth exploring (and storing these)
- noticing which websites/url domains tend to yield helpful results, that are reliably current, up to date, etc, and storing these in a growing list of reliable sites to search for information.
- learning certain words or phrases to search for, in order to obtain the most useful information for identifying real-time trends.
- learning how to use the results of search queries to decide on follow up queries, to recursively track down the best, most useful info.
- learns how to be 'efficient' in web search tool use, so as not to incur API costs for irrelevant information
- learns how to use grok's 'x_search' tool efficiently to just look for the most important data, and not waste API costs on searches not likely to be useful.

The system should have a "learning mode" where it explores different hypotheses related making profits, tried to make "hypothetical" profits, and learns from successes and failures, always updating its knowledge.

I am not sure whether it would be useful to involve any "Agent" toolkits (i.e. OpenAI Agents SDK) or whether standard LLM API calls are sufficient for this syetm. Would appreciate input on this design decision.

The plan should include several ways for controlling the system, to control which models are used for which purposes, 
for controlling the reasoning levels, setting maximum allowable costs,
methods for easy monitoring of all API costs, ways to monitor the inputs and outputs to each model, 
methods for viewing what the model is "learning" at it evolves (and for human tinkering with the learned knowldge), 

...these ways should at least include '.env' parameters, as well as a web UI/dashboard for eacy viewing/controlling.

I would like to use OpenAI "responses" API when possible (and Grok responses too).
It should be easy to swithc back and forth between different models (OpenAI, Gemini, Grok) and still use web search tool.
Of course Grok is only one with x_search tool.

Please use context7 mcp server whenever possible, for design and implementation, in order to ensure using the latest, most up to date documentation and examples whenever possible.

My '.env' file contains:
```
ALPACA_API_KEY
ALPACA_SECRET_KEY
SCHWAB_APP_KEY
SCHWAB_APP_SECRET
OPENAI_API_KEY
GOOGLE_API_KEY
XAI_API_KEY
```