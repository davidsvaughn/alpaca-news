1. LEVELONE_EQUITIES
 

Level One Equities Request

| Streamer Contract Name | Subfield | Type    | Length   | Description                                                                                                          |
| ---------------------- | -------- | ------- | -------- | -------------------------------------------------------------------------------------------------------------------- |
| service                |          | String  | Variable | LEVELONE_EQUITIES                                                                                                    |
| command                |          | String  | Variable | SUBS, UNSUBS, ADD, VIEW                                                                                              |
| requestid              |          | Integer | Variable | Unique number that will identify this request                                                                        |
| SchwabClientCustomerId |          | String  | Variable | `schwabClientCustomerId` as found in GET User Preference endpoint                                                    |
| SchwabClientCorrelId   |          | String  | Variable | Unique identifier attached to requests and messages that allows reference to a particular transaction or event chain |
| parameters             | keys     | String  | Variable | Schwab-standard symbols in uppercase and separated by commas (e.g., AAPL,TSLA,IBM)                                   |
| parameters             | fields   | String  | Variable | See the LEVELONE_EQUITIES Field Definition table                                                                     |



LEVELONE_EQUITIES Request Example:
```json
{
 "requests": [
  {
   "service": "LEVELONE_EQUITIES",
   "requestid": 1,
   "command": "SUBS",
   "SchwabClientCustomerId": "Someone",
   "SchwabClientCorrelId": "29bdf6d-b9d0-46dd-8786-424e1577bd",
   "parameters": {
    "keys": "SCHW,AAPL,SPY",
    "fields": "0,1,2,3,4,5,8,10 "
   }
  }
 ]
}
```

Response Field Definitions
Outside of fields that can be subscribed to, Streamer also returns initial data that indicates whether the data is real time or NFL (delayed).

| Field Name    | Type    | Field Description                   | Notes, Examples Source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------- | ------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| key           | String  | Usually this is the symbol          | AAPL                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| delayed       | boolean | Whether data is from the SIP or NFL | **false:** data is from a SIP. SIP (Securities Information Processor) collects trade and quote data from multiple exchanges and consolidates them into a single source. <br><br> **true:** data is from an NFL source. NFL (Non-Fee Liable) either means delayed data (often options, futures, futures options) or real-time data from a subset of exchanges that does not include all markets in the National Plan. Delayed quotes do not represent the most recent last or bid/ask; subset real-time quotes may also not contain the most recent last or bid/ask. |
| assetMainType | String  | Asset Type                          | BOND, EQUITY, ETF, EXTENDED, FOREX, FUTURE, FUTURE_OPTION, FUNDAMENTAL, INDEX, INDICATOR, MUTUAL_FUND, OPTION, UNKNOWN                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| assetSubType  | String  | Asset sub type                      | ADR, CEF, COE, ETF, ETN, GDR, OEF, PRF, RGT, UIT, WAR                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| cusip         | String  | 9 digits CUSIP                      | CUSIP number for the instrument, such as **594918104**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |




LEVELONE_EQUITIES Response Example:
```json
{
 "data": [
  {
   "service": "LEVELONE_EQUITIES",
   "timestamp": 1714949592301,
   "command": "SUBS",
   "content": [
    {
     "key": "SCHW",
     "delayed": false,
     "assetMainType": "EQUITY",
     "assetSubType": "COE",
     "cusip": "808513105",
     "1": 76.08,
     "2": 76.49,
     "3": 76.44,
     "4": 3,
     "5": 1,
     "8": 5414735,
     "10": 76.47
    },
    {
     "key": "AAPL",
     "delayed": false,
     "assetMainType": "EQUITY",
     "assetSubType": "COE",
     "cusip": "037833100",
     "1": 183.75,
     "2": 183.8,
     "3": 183.8,
     "4": 1,
     "5": 2,
     "8": 163224109,
     "10": 187
    },
    {
     "key": "SPY",
     "delayed": false,
     "assetMainType": "EQUITY",
     "assetSubType": "ETF",
     "cusip": "78462F103",
     "1": 512.3,
     "2": 512.32,
     "3": 511.29,
     "4": 8,
     "5": 1,
     "8": 72756709,
     "10": 512.55
    }
   ]
  }
 ]
}
```


| Fields | Field Name                        | Type    | Field Description                                                                                                                                                           | Notes, Examples Source                                                                                                                                                                              |
| ------ | --------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0      | Symbol                            | String  | Ticker symbol in upper case.                                                                                                                                                |                                                                                                                                                                                                     |
| 1      | Bid Price                         | double  | Current Bid Price                                                                                                                                                           |                                                                                                                                                                                                     |
| 2      | Ask Price                         | double  | Current Ask Price                                                                                                                                                           |                                                                                                                                                                                                     |
| 3      | Last Price                        | double  | Price at which the last trade was matched                                                                                                                                   |                                                                                                                                                                                                     |
| 4      | Bid Size                          | int     | Number of shares for bid                                                                                                                                                    | Units are "lots" (typically 100 shares per lot). Note for NFL data this field can be 0 with a non-zero bid price representing a bid size of less than 100 shares.                                   |
| 5      | Ask Size                          | int     | Number of shares for ask                                                                                                                                                    | See bid size notes.                                                                                                                                                                                 |
| 6      | Ask ID                            | char    | Exchange with the ask                                                                                                                                                       |                                                                                                                                                                                                     |
| 7      | Bid ID                            | char    | Exchange with the bid                                                                                                                                                       |                                                                                                                                                                                                     |
| 8      | Total Volume                      | long    | Aggregated shares traded throughout the day, including pre/post market hours.                                                                                               | Volume is set to zero at 7:28am ET.                                                                                                                                                                 |
| 9      | Last Size                         | long    | Number of shares traded with last trade                                                                                                                                     | Units are shares                                                                                                                                                                                    |
| 10     | High Price                        | double  | Day's high trade price                                                                                                                                                      | According to industry standard, only regular session trades set the High and Low. If a stock does not trade in the regular session, high and low will be zero. High/Low reset to ZERO at 3:30am ET. |
| 11     | Low Price                         | double  | Day's low trade price                                                                                                                                                       | See High Price notes                                                                                                                                                                                |
| 12     | Close Price                       | double  | Previous day's closing price                                                                                                                                                | Closing prices are updated from the DB at 3:30 AM ET.                                                                                                                                               |
| 13     | Exchange ID                       | char    | Primary "listing" Exchange                                                                                                                                                  | As long as the symbol is valid, this data is always present. This field is updated every time the closing prices are loaded from DB.                                                                |
| 14     | Marginable                        | boolean | Stock approved by the Federal Reserve and an investor's broker as being eligible for providing collateral for margin debt.                                                  |                                                                                                                                                                                                     |
| 15     | Description                       | String  | A company, index or fund name                                                                                                                                               | Once per day descriptions are loaded from the database at 7:29:50 AM ET.                                                                                                                            |
| 16     | Last ID                           | char    | Exchange where last trade was executed                                                                                                                                      |                                                                                                                                                                                                     |
| 17     | Open Price                        | double  | Day's Open Price. According to industry standard, only regular session trades set the open. If a stock does not trade during the regular session, then the open price is 0. | In the pre-market session, open is blank because pre-market session trades do not set the open. Open is set to ZERO at 3:30am ET.                                                                   |
| 18     | Net Change                        | double  |                                                                                                                                                                             | LastPrice - ClosePrice. If close is zero, change will be zero.                                                                                                                                      |
| 19     | 52 Week High                      | double  | Highest price traded in the past 12 months or 52 weeks                                                                                                                      | Calculated by merging intraday high (from fh) and 52-week high (from db).                                                                                                                           |
| 20     | 52 Week Low                       | double  | Lowest price traded in the past 12 months or 52 weeks                                                                                                                       | Calculated by merging intraday low (from fh) and 52-week low (from db).                                                                                                                             |
| 21     | PE Ratio                          | double  | Price-to-earnings ratio. Price of a share divided by earnings-per-share.                                                                                                    | Theoretically could stream since price changes intraday, but implementation uses closing price so it does not stream throughout the day.                                                            |
| 22     | Annual Dividend Amount            | double  | Annual Dividend Amount                                                                                                                                                      |                                                                                                                                                                                                     |
| 23     | Dividend Yield                    | double  | Dividend Yield                                                                                                                                                              |                                                                                                                                                                                                     |
| 24     | NAV                               | double  | Mutual Fund Net Asset Value                                                                                                                                                 | Loaded various times after market close.                                                                                                                                                            |
| 25     | Exchange Name                     | String  | Display name of exchange                                                                                                                                                    |                                                                                                                                                                                                     |
| 26     | Dividend Date                     | String  |                                                                                                                                                                             |                                                                                                                                                                                                     |
| 27     | Regular Market Quote              | boolean |                                                                                                                                                                             | Is last quote a regular quote                                                                                                                                                                       |
| 28     | Regular Market Trade              | boolean |                                                                                                                                                                             | Is last trade a regular trade                                                                                                                                                                       |
| 29     | Regular Market Last Price         | double  |                                                                                                                                                                             | Only records regular trade                                                                                                                                                                          |
| 30     | Regular Market Last Size          | integer |                                                                                                                                                                             | Currently realize/100, only records regular trade                                                                                                                                                   |
| 31     | Regular Market Net Change         | double  |                                                                                                                                                                             | RegularMarketLastPrice - ClosePrice                                                                                                                                                                 |
| 32     | Security Status                   | String  |                                                                                                                                                                             | Indicates a symbol's current trading status: Normal, Halted, Closed                                                                                                                                 |
| 33     | Mark Price                        | double  | Mark Price                                                                                                                                                                  |                                                                                                                                                                                                     |
| 34     | Quote Time in Long                | Long    | Last time a bid or ask updated in milliseconds since Epoch                                                                                                                  | Difference in milliseconds between event time and midnight Jan 1 1970 UTC.                                                                                                                          |
| 35     | Trade Time in Long                | Long    | Last trade time in milliseconds since Epoch                                                                                                                                 | Difference in milliseconds between event time and midnight Jan 1 1970 UTC.                                                                                                                          |
| 36     | Regular Market Trade Time in Long | Long    | Regular market trade time in milliseconds since Epoch                                                                                                                       | Difference in milliseconds between event time and midnight Jan 1 1970 UTC.                                                                                                                          |
| 37     | Bid Time                          | long    | Last bid time in milliseconds since Epoch                                                                                                                                   | Difference in milliseconds between event time and midnight Jan 1 1970 UTC.                                                                                                                          |
| 38     | Ask Time                          | long    | Last ask time in milliseconds since Epoch                                                                                                                                   | Difference in milliseconds between event time and midnight Jan 1 1970 UTC.                                                                                                                          |
| 39     | Ask MIC ID                        | String  | 4-character Market Identifier Code                                                                                                                                          |                                                                                                                                                                                                     |
| 40     | Bid MIC ID                        | String  | 4-character Market Identifier Code                                                                                                                                          |                                                                                                                                                                                                     |
| 41     | Last MIC ID                       | String  | 4-character Market Identifier Code                                                                                                                                          |                                                                                                                                                                                                     |
| 42     | Net Percent Change                | double  | Net Percentage Change                                                                                                                                                       | NetChange / ClosePrice * 100                                                                                                                                                                        |
| 43     | Regular Market Percent Change     | double  | Regular market hours percentage change                                                                                                                                      | RegularMarketNetChange / ClosePrice * 100                                                                                                                                                           |
| 44     | Mark Price Net Change             | double  | Mark price net change                                                                                                                                                       | 7.97                                                                                                                                                                                                |
| 45     | Mark Price Percent Change         | double  | Mark price percentage change                                                                                                                                                | 4.2358                                                                                                                                                                                              |
| 46     | Hard to Borrow Quantity           | integer |                                                                                                                                                                             | -1 = NULL. ≥0 is valid quantity.                                                                                                                                                                    |
| 47     | Hard To Borrow Rate               | double  |                                                                                                                                                                             | null = NULL. Valid range = -99,999.999 to +99,999.999                                                                                                                                               |
| 48     | Hard to Borrow                    | integer |                                                                                                                                                                             | -1 = NULL, 1 = true, 0 = false                                                                                                                                                                      |
| 49     | Shortable                         | integer |                                                                                                                                                                             | -1 = NULL, 1 = true, 0 = false                                                                                                                                                                      |
| 50     | Post-Market Net Change            | double  | Change in price since end of regular session (typically 4:00pm)                                                                                                             | PostMarketLastPrice - RegularMarketLastPrice                                                                                                                                                        |
| 51     | Post-Market Percent Change        | double  | Percent change since end of regular session (typically 4:00pm)                                                                                                              | PostMarketNetChange / RegularMarketLastPrice * 100                                                                                                                                                  |


Row **13 (Exchange ID)** includes a small lookup table for the exchange codes. Rendered cleanly in Markdown:

| Exchange    | Code | Realtime/NFL  |
| ----------- | ---- | ------------- |
| AMEX        | A    | Both          |
| Indicator   | :    | Realtime Only |
| Indices     | 0    | Realtime Only |
| Mutual Fund | 3    | Realtime Only |
| NASDAQ      | Q    | Both          |
| NYSE        | N    | Both          |
| Pacific     | P    | Both          |
| Pinks       | 9    | Realtime Only |
| OTCBB       | U    | Realtime Only |

Meaning: the **`Exchange ID` field (char)** in the stream will contain one of those single-character codes indicating the listing exchange.
