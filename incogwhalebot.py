import tweepy
from incognitosdk.Types import *
from incognitosdk.Incognito import *
from incognitosdk.Utilities import *
import json
import logging
import sys
from collections import deque
from copy import deepcopy
from telegram.client import Telegram
import os
import initialization
import threading

FAULT_TOLERANCE_ENABLED = True
iExc = 0


def testFaultTolerance(bound):
    global iExc

    if iExc < bound and FAULT_TOLERANCE_ENABLED:
        iExc = iExc + 1
        logging.info(f'iExc = {iExc}')
        raise Exception("Fault-tolerance test")


lock = threading.Event()


def trackAllTxs():
    global incognitoApi, publicApi, tokenDict, lock

    while True:
        try:
            lock.clear()

            logging.info("Service starts...")

            incognitoApi = Incognito()
            publicApi = incognitoApi.Public

            if __debug__:
                testFaultTolerance(3)

            tokenList = publicApi.get_token_list()
            logging.debug(tokenList)

            tokenDict = {x['TokenID']: x for x in tokenList.json()['Result']}
            tokenDict.update({PRV_ID: {'Symbol': 'PRV', 'PSymbol': 'PRV', 'PDecimals': 9}})

            publicApi.subscribe(SubscriptionType.NewBeaconBlock, [], receiveTx)
            for shard in range(0, 8):
                publicApi.subscribe(SubscriptionType.NewShardBlock, [shard], receiveTx)

            lock.wait()

            logging.info("Lock resolved...")

        except (SystemExit, KeyboardInterrupt):
            if not __debug__:
                tg.stop()
            exit()
        except:
            logging.exception("Error within main loop: ")
        finally:
            try:
                logging.info("Destroying Incognito object...")
                incognitoApi.destroy()
            except:
                logging.exception("Error while destroying: ")


def receiveTx(subscriptionType, result):
    global beaconHeight

    if __debug__:
        testFaultTolerance(6)

    if subscriptionType == SubscriptionType.NewShardBlock:
        shardBlock = result
        logging.info(
            f'ShardID = {shardBlock.get_shard_id()}, Height = {shardBlock.get_block_height()}, BeaconHeight = {beaconHeight - 1},  Txs = {shardBlock.get_tx_hashes()}')
        for txHash in shardBlock.get_tx_hashes():
            publicApi.subscribe(SubscriptionType.PendingTransaction, [txHash], receiveTx)
    elif subscriptionType == SubscriptionType.PendingTransaction:
        tx = result
        metadata = tx.get_metadata()
        logging.info(f'Metadata = {metadata}')
        if 'Type' in metadata:
            transactionType = metadata['Type']
            if transactionType == TransactionType.Erc20Shielding or transactionType == TransactionType.Shielding:
                tokenData = tx.get_privacy_custom_token_data()
                shieldedAmount = tokenData['Amount']
                shieldedTokenId = tokenData['PropertyID']
                if shieldedTokenId in tokenDict:
                    token = tokenDict[shieldedTokenId]
                    processedBeaconHeight = beaconHeight - 1

                    pdePoolPairs = getPdeState(processedBeaconHeight)

                    poolId = f'pdepool-{processedBeaconHeight}-{PRV_ID}-{shieldedTokenId}'
                    if poolId in pdePoolPairs:
                        pair = pdePoolPairs[poolId]
                        sellAmountNormalized, buyAmountNormalized, possibleGainPercentage, _, _ = calculateGainPercentage(
                            shieldedAmount,
                            pair, False)

                        logging.info(pair)
                        logging.info(
                            f'{sellAmountNormalized} {token["Symbol"]} was shielded at {processedBeaconHeight}th beacon. Possible Buy ={buyAmountNormalized:.8f}, PRV Increase= {possibleGainPercentage:.2f}%')

                        if possibleGainPercentage > LOWEST_GAIN_LIMIT:
                            message = f'🚨🛡️ {c(sellAmountNormalized)} #{token["Symbol"]} shielded. If it was sold ' \
                                      f'completely, PRV/{token["Symbol"]} ticker would increase by 🔼{p(possibleGainPercentage)}%' \
                                      f'\n\nTx: incscan.io/blockchain/transactions/{tx.get_hash()}'
                            publishMessage(message)

            elif transactionType == TransactionType.Trade and metadata['TradeStatus'] == TradeStatus.Accepted:
                requestTxHash = metadata['RequestedTxID']

                # to stay in the safe side, in fact, this loop is not required.
                tradeOnRadar = None
                for bh in range(beaconHeight - 1, beaconHeight - 6, -1):
                    poolBeaconHeight = bh - 1
                    oldPdePoolPairs = deepcopy(getPdeState(poolBeaconHeight))
                    trades = getPdeTrades(bh)

                    # update oldPdePairs by accepted trades until this trade
                    for trade in trades:
                        if trade['RequestedTxID'] != requestTxHash:
                            for tradePath in trade["TradePaths"]:
                                poolId = f'pdepool-{poolBeaconHeight}-{tradePath["Token1IDStr"]}-{tradePath["Token2IDStr"]}'
                                if poolId in oldPdePoolPairs:
                                    pair = oldPdePoolPairs[poolId]
                                    if tradePath["TokenIDToBuyStr"] == tradePath["Token1IDStr"]:
                                        pair["Token1PoolValue"] = pair["Token1PoolValue"] - tradePath[
                                            "ReceiveAmount"]
                                        pair["Token2PoolValue"] = pair["Token2PoolValue"] + tradePath[
                                            "SellAmount"]
                                    else:
                                        pair["Token1PoolValue"] = pair["Token1PoolValue"] + tradePath[
                                            "SellAmount"]
                                        pair["Token2PoolValue"] = pair["Token2PoolValue"] - tradePath[
                                            "ReceiveAmount"]
                        else:
                            tradeOnRadar = trade
                            break

                    if tradeOnRadar is not None:
                        break

                if tradeOnRadar is not None:
                    initialSellAmount = None
                    initialSellTokenId = None
                    buyAmountNormalized = None
                    lastBuyTokenId = None
                    priceChange = ""
                    priceChangeForLog = ""
                    crossPrice = None
                    oldCrossPrice = None
                    numTradePath = 0
                    gainPercentage = 0
                    for tradePath in tradeOnRadar["TradePaths"]:
                        poolId = f'pdepool-{poolBeaconHeight}-{tradePath["Token1IDStr"]}-{tradePath["Token2IDStr"]}'
                        if poolId in oldPdePoolPairs:
                            pair = oldPdePoolPairs[poolId]
                            prvSold = tradePath["TokenIDToBuyStr"] != tradePath["Token1IDStr"]
                            sellAmountNormalized, buyAmountNormalized, gainPercentage, oldPrice, price = calculateGainPercentage(
                                tradePath["SellAmount"], pair, prvSold)

                            messageTemplate = createTicker(tradePath["Token1IDStr"], tradePath["Token2IDStr"],
                                                           gainPercentage, price, prvSold)
                            priceChange = priceChange + messageTemplate.format("")
                            priceChangeForLog = priceChangeForLog + messageTemplate.format(
                                f'-{gainPercentage},{oldPrice}')

                            if initialSellAmount is None:
                                initialSellAmount = sellAmountNormalized
                                initialSellTokenId = tradePath["Token1IDStr"] if prvSold else tradePath[
                                    "Token2IDStr"]
                                crossPrice = price
                                oldCrossPrice = oldPrice
                            else:
                                crossPrice = price / crossPrice
                                oldCrossPrice = oldPrice / oldCrossPrice

                            lastBuyTokenId = tradePath["Token2IDStr"] if prvSold else tradePath["Token1IDStr"]
                            numTradePath = numTradePath + 1

                    crossGainPercentage = 0
                    if numTradePath > 1:
                        crossGainPercentage = (crossPrice / oldCrossPrice - 1) * 100
                        messageTemplate = createTicker(initialSellTokenId, lastBuyTokenId,
                                                       abs(crossGainPercentage), crossPrice, True)
                        priceChange = messageTemplate.format("") + priceChange
                        priceChangeForLog = messageTemplate.format(
                            f'-{crossGainPercentage},{oldCrossPrice}') + priceChangeForLog

                    message = f'🚨️🔄 {c(initialSellAmount)} #{tokenDict[initialSellTokenId]["Symbol"]} ➡️  {c(buyAmountNormalized)} #{tokenDict[lastBuyTokenId]["Symbol"]}\n' \
                              f'Tickers:\n{{}}' \
                              f'\nTx: incscan.io/blockchain/transactions/{tx.get_hash()}'

                    logging.info(message.format(priceChangeForLog))

                    if abs(crossGainPercentage) > LOWEST_GAIN_LIMIT or (
                            numTradePath == 1 and abs(gainPercentage) > LOWEST_GAIN_LIMIT):
                        publishMessage(message.format(priceChange))

    elif subscriptionType == SubscriptionType.NewBeaconBlock:
        shardBlock = result
        beaconHeight = shardBlock.get_block_height()
        logging.info(f'BEACON Height = {beaconHeight}')
    else:
        logging.info(result)


def createTicker(sellTokenId, buyTokenId, gainPercentage, price, down):
    return f'{tokenDict[sellTokenId]["Symbol"]}/{tokenDict[buyTokenId]["Symbol"]}: ' \
           f'{c(price)} {"🔽-" if down else "🔼"}{p(gainPercentage)}%{{}}, ' \
           f'{tokenDict[buyTokenId]["Symbol"]}/{tokenDict[sellTokenId]["Symbol"]}: {c(1 / price)}\n'


def calculateGainPercentage(amount, pair, prvSold):
    buyPoolAmount = pair['Token1PoolValue']
    sellPoolAmount = pair['Token2PoolValue']
    buyTokenPrecision = tokenDict[pair["Token1IDStr"]]['PDecimals']
    sellTokenPrecision = tokenDict[pair["Token2IDStr"]]['PDecimals']

    if prvSold:
        buyPoolAmount = pair['Token2PoolValue']
        sellPoolAmount = pair['Token1PoolValue']
        buyTokenPrecision = tokenDict[pair["Token2IDStr"]]['PDecimals']
        sellTokenPrecision = tokenDict[pair["Token1IDStr"]]['PDecimals']

    buyPoolAmountNormalized = coin(buyPoolAmount, buyTokenPrecision, False)
    sellPoolAmountNormalized = coin(sellPoolAmount, sellTokenPrecision, False)

    sellAmount = coin(amount, sellTokenPrecision, False)
    buyAmount = calculateBuyAmount(sellAmount, sellPoolAmountNormalized, buyPoolAmountNormalized)
    oldPrice = sellPoolAmountNormalized / buyPoolAmountNormalized
    newPrice = (sellPoolAmountNormalized + sellAmount) / (buyPoolAmountNormalized - buyAmount)
    possibleGainPercentage = (newPrice / oldPrice - 1) * 100

    # always return price in terms of PRV
    return sellAmount, buyAmount, possibleGainPercentage, 1 / oldPrice if prvSold else oldPrice, 1 / newPrice if prvSold else newPrice


def publishMessage(message):
    if not __debug__:
        try:
            result = tg.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=message
            )
            result.wait()
        except:
            logging.exception("Exception when telegramming:")

        try:
            twitterApi.update_status(message[:280])
        except:
            logging.exception("Exception when tweeting:")


def getPdeState(beaconHeight):
    global recentPdeStates

    # first try the cache
    pdePoolPairs = None
    for i in range(len(recentPdeStates)):
        if recentPdeStates[i].params().get_beacon_height() == beaconHeight:
            pdePoolPairs = recentPdeStates[i].get_pde_pool_pairs()
    if pdePoolPairs is None:
        pdeState = publicApi.get_pde_state(beaconHeight)
        pdePoolPairs = pdeState.get_pde_pool_pairs()
        recentPdeStates.appendleft(pdeState)
    return pdePoolPairs


def getPdeTrades(beaconHeight):
    global recentPdeTrades

    # first try the cache
    pdeAcceptedTrades = None
    for i in range(len(recentPdeTrades)):
        if recentPdeTrades[i].params().get_beacon_height() == beaconHeight:
            pdeAcceptedTrades = recentPdeTrades[i].get_accepted_trades()
    if pdeAcceptedTrades is None:
        pdeTrades = publicApi.get_pde_inst(beaconHeight)
        pdeAcceptedTrades = pdeTrades.get_accepted_trades()
        recentPdeTrades.appendleft(pdeTrades)
    return pdeAcceptedTrades


##### MAIN ########
with open('config/telegram.json') as f:
    telegramKeys = json.load(f)

if __debug__:
    MODE = "DEBUG"
    TELEGRAM_CHANNEL_ID = telegramKeys["devChannelId"]
else:
    MODE = "PRODUCTION"
    TELEGRAM_CHANNEL_ID = telegramKeys["prodChannelId"]

print(f'Your are in {MODE} mode. Press enter to continue:')

if 'RUNNING_IN_IDE' not in os.environ:
    input()

if not __debug__:
    # Twitter setup
    with open('config/twitter.json') as f:
        twitterKeys = json.load(f)

    auth = tweepy.OAuthHandler(twitterKeys["consumer_key"], twitterKeys["consumer_secret"])
    auth.set_access_token(twitterKeys["access_token_key"], twitterKeys["access_token_secret"])

    twitterApi = tweepy.API(auth)

    # Telegram setup
    tg = Telegram(
        api_id=telegramKeys["api_id"],
        api_hash=telegramKeys["api_hash"],
        # phone='',  # you can pass 'bot_token' instead,
        bot_token=telegramKeys["bot_token"],  # nitowhalemanagerbot token
        database_encryption_key=telegramKeys["database_encryption_key"],
    )
    tg.login()

    # if this is the first run, library needs to preload all chats
    # otherwise the message will not be sent
    result = tg.get_chats()
    result.wait()


def globalErrorHandler(exceptionType, value, stackTrace):
    logging.exception("Globally handled exception")
    lock.set()


sys.excepthook = globalErrorHandler

incognitoApi = None
publicApi = None
tokenDict = None

recentPdeStates = deque(maxlen=5)
recentPdeTrades = deque(maxlen=5)

LOWEST_GAIN_LIMIT = 1

beaconHeight = 0
trackAllTxs()
