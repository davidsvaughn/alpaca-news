https://developer.schwab.com/products/trader-api--individual/details/specifications/Market%20Data%20Production

https://developer.schwab.com/products/trader-api--individual/details/documentation/Market%20Data%20Production


Scopes are used to grant an application different levels of access to data on behalf of the end user. Each API may declare one or more scopes.

API requires the following scopes. Select which ones you want to grant to Swagger UI.

oauth (OAuth2, authorizationCode)
Authorization URL: https://api.schwabapi.com/v1/oauth/authorize?response_type=code&client_id=fnB6k1X6JSFlQHravRt6T9m86AZlkD04&scope=readonly&redirect_uri=https://developer.schwab.com/oauth2-redirect.html

Token URL: https://api.schwabapi.com/v1/oauth/token

Flow: authorizationCode

client_id:
client_secret


You've selected the following accounts to link to Market Data Production Try It Now.
Individual ...686


curl -X 'GET' \
  'https://api.schwabapi.com/marketdata/v1/NVDA/quotes?fields=quote%2Creference' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer I0.b2F1dGgyLmNkYy5zY2h3YWIuY29t.JSfFWATEq8RLjfaKlnq29nyspdx6Em_A3eTMLi9iI-c@'


curl -X 'GET' \
  'https://api.schwabapi.com/marketdata/v1/pricehistory?symbol=NVDA&periodType=day&needExtendedHoursData=true&needPreviousClose=true' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer I0.b2F1dGgyLmNkYy5zY2h3YWIuY29t.JSfFWATEq8RLjfaKlnq29nyspdx6Em_A3eTMLi9iI-c@'