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

FAULT_TOLERANCE_ENABLED = False
iExc = 0


def testFaultTolerance(bound):
    global iExc

    if iExc < bound and FAULT_TOLERANCE_ENABLED:
        iExc = iExc + 1
        logging.info(f'iExc = {iExc}')
        raise Exception("Fault-tolerance test")


lock = threading.Event()


def trackAllTxs():
    global incognitoApi, publicApi, tokenDict, lock, beaconHeight

    while True:
        try:
            lock.clear()

            logging.info("Service starts...")

            inconfig = Incognito.Config()
            inconfig.WsUrl = "ws://localhost:19334"
            inconfig.RpcUrl = "http://localhost:9534/"
            inconfig.TokenListUrl = "https://genebil.com/ptoken.json"
            incognitoApi = Incognito(inconfig)

            # inconfig = Incognito.Config()
            # inconfig.WsUrl = "ws://fullnode.incognito.best:19334"
            # inconfig.RpcUrl = "https://fullnode.incognito.best"
            # inconfig.TokenListUrl = "https://genebil.com/ptoken.json"
            # incognitoApi = Incognito(inconfig)

            publicApi = incognitoApi.Public
            beaconHeight = publicApi.get_beacon_height()

            if __debug__:
                testFaultTolerance(3)

            tokenDict = publicApi.TokenDict
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
    global beaconHeight, numTotalStaker

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
                moneyUnlocked(tx, tokenData['Amount'], tokenData['PropertyID'], "🛡", "shielded")

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
                                pair = getPool(oldPdePoolPairs, poolBeaconHeight, tradePath["Token2IDStr"],
                                               tradePath["Token1IDStr"])
                                if pair is not None:
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
                    liquidityFailed = True
                    for tradePath in tradeOnRadar["TradePaths"]:
                        pair = getPool(oldPdePoolPairs, poolBeaconHeight, tradePath["Token2IDStr"],
                                       tradePath["Token1IDStr"])
                        if pair is not None:
                            prvSold = tradePath["TokenIDToBuyStr"] != tradePath["Token1IDStr"]
                            sellAmountNormalized, buyAmountNormalized, gainPercentage, oldPrice, price = calculateGainPercentage(
                                tradePath["SellAmount"], pair, prvSold)

                            if hasEnoughLiquidity(tradePath["Token2IDStr"], oldPdePoolPairs, poolBeaconHeight) and abs(
                                    gainPercentage) > LOWEST_GAIN_LIMIT:
                                liquidityFailed = False

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

                    if liquidityFailed:
                        return

                    crossGainPercentage = 0
                    if numTradePath > 1:
                        crossGainPercentage = (crossPrice / oldCrossPrice - 1) * 100
                        messageTemplate = createTicker(initialSellTokenId, lastBuyTokenId,
                                                       abs(crossGainPercentage), crossPrice, True)
                        priceChange = messageTemplate.format("") + priceChange
                        priceChangeForLog = messageTemplate.format(
                            f'-{crossGainPercentage},{oldCrossPrice}') + priceChangeForLog

                    message = f'🚨️🔄 {c(initialSellAmount)} #{getCoin(initialSellTokenId)["Symbol"]} ➡️  {c(buyAmountNormalized)} #{getCoin(lastBuyTokenId)["Symbol"]}\n' \
                              f'Tickers:\n{{}}' \
                              f'\nTx: incscan.io/tx/{tx.get_hash()}'

                    logging.info(message.format(priceChangeForLog))

                    if abs(crossGainPercentage) > LOWEST_GAIN_LIMIT or (
                            numTradePath == 1 and abs(gainPercentage) > LOWEST_GAIN_LIMIT):
                        publishMessage(message.format(priceChange))

            elif transactionType == TransactionType.Unstake:
                message = f'🚨❌⛏️ A validator node has finished🔚 staking. The number of remaining validator nodes is {decreaseTotalStaker() - 7}.' \
                          f'\n\nTx: incscan.io/tx/{tx.get_hash()}'
                publishMessage(message)

            elif transactionType == TransactionType.RemoveLiquidity:
                unlockedTokenId = metadata['TokenIDStr']

                if unlockedTokenId != PRV_ID:
                    unlockedAmount = tx.get_privacy_custom_token_data()['Amount']
                else:
                    logging.info(tx)
                    unlockedAmount = tx.get_proof_detail_output_coin_value_prv()

                moneyUnlocked(tx, unlockedAmount, unlockedTokenId, "❌💵", "removed from pDEX")

    elif subscriptionType == SubscriptionType.NewBeaconBlock:
        clearTotalStaker()
        beaconBlock = result
        beaconHeight = beaconBlock.get_block_height()
        logging.info(f'BEACON Height = {beaconHeight}')
    else:
        logging.info(result)


stakerLock = threading.Lock()


def clearTotalStaker():
    global numTotalStaker
    with stakerLock:
        numTotalStaker = -1


def _getTotalStaker():
    global numTotalStaker
    if numTotalStaker < 0:
        numTotalStaker = publicApi.get_total_staker()
    return numTotalStaker


def increaseTotalStaker():
    global numTotalStaker
    with stakerLock:
        numTotalStaker = _getTotalStaker() + 1
        return numTotalStaker


def decreaseTotalStaker():
    global numTotalStaker
    with stakerLock:
        numTotalStaker = _getTotalStaker() - 1
        return numTotalStaker


def moneyUnlocked(tx, unlockedAmount, unlockedTokenId, icons, verb):
    global beaconHeight

    token = getCoin(unlockedTokenId)
    if token is not None:
        processedBeaconHeight = beaconHeight - 1

        if unlockedTokenId == PRV_ID:
            pdePoolPairs = getPdeState(processedBeaconHeight)
            pair = getPool(pdePoolPairs, processedBeaconHeight, USDT_ID)

            sellAmountNormalized, _, gainPercentage, _, _ = calculateGainPercentage(unlockedAmount, pair, True)

            message = f'🚨{icons}️ {sellAmountNormalized} #{getCoin(unlockedTokenId)["Symbol"]} {verb}.' \
                      f'\n\nTx: incscan.io/tx/{tx.get_hash()}'
            logging.info(message)

            if abs(gainPercentage) > LOWEST_GAIN_LIMIT:
                publishMessage(message)

            return

        pdePoolPairs = getPdeState(processedBeaconHeight)
        pair = getPool(pdePoolPairs, processedBeaconHeight, unlockedTokenId)
        if pair is not None:
            if not hasEnoughLiquidity(unlockedTokenId, pdePoolPairs, processedBeaconHeight):
                return

            sellAmountNormalized, buyAmountNormalized, possibleGainPercentage, _, _ = calculateGainPercentage(
                unlockedAmount,
                pair, False)

            logging.info(pair)
            logging.info(
                f'{sellAmountNormalized} {token["Symbol"]} was {verb} at {processedBeaconHeight}th beacon. Possible Buy ={buyAmountNormalized:.8f}, PRV Increase= {possibleGainPercentage:.2f}%')

            if possibleGainPercentage > LOWEST_GAIN_LIMIT:
                message = f'🚨{icons} {c(sellAmountNormalized)} #{token["Symbol"]} {verb}. If it was sold ' \
                          f'completely, PRV/{token["Symbol"]} ticker would increase by 🔼{p(possibleGainPercentage)}%' \
                          f'\n\nTx: incscan.io/tx/{tx.get_hash()}'
                publishMessage(message)


def createTicker(sellTokenId, buyTokenId, gainPercentage, price, down):
    return f'{getCoin(sellTokenId)["Symbol"]}/{getCoin(buyTokenId)["Symbol"]}: ' \
           f'{c(price)} {"🔻-" if down else "🔼"}{p(gainPercentage)}%{{}}, ' \
           f'{getCoin(buyTokenId)["Symbol"]}/{getCoin(sellTokenId)["Symbol"]}: {c(1 / price)}\n'


def calculateGainPercentage(amount, pair, prvSold):
    buyPoolAmount = pair['Token1PoolValue']
    sellPoolAmount = pair['Token2PoolValue']
    buyTokenPrecision = getCoin(pair["Token1IDStr"])['PDecimals']
    sellTokenPrecision = getCoin(pair["Token2IDStr"])['PDecimals']

    if prvSold:
        buyPoolAmount = pair['Token2PoolValue']
        sellPoolAmount = pair['Token1PoolValue']
        buyTokenPrecision = getCoin(pair["Token2IDStr"])['PDecimals']
        sellTokenPrecision = getCoin(pair["Token1IDStr"])['PDecimals']

    buyPoolAmountNormalized = coin(buyPoolAmount, buyTokenPrecision, False)
    sellPoolAmountNormalized = coin(sellPoolAmount, sellTokenPrecision, False)

    sellAmount = coin(amount, sellTokenPrecision, False)
    buyAmount = calculateBuyAmount(sellAmount, sellPoolAmountNormalized, buyPoolAmountNormalized)
    oldPrice = sellPoolAmountNormalized / buyPoolAmountNormalized
    newPrice = (sellPoolAmountNormalized + sellAmount) / (buyPoolAmountNormalized - buyAmount)
    possibleGainPercentage = (newPrice / oldPrice - 1) * 100

    # always return price in terms of PRV
    return sellAmount, buyAmount, possibleGainPercentage, 1 / oldPrice if prvSold else oldPrice, 1 / newPrice if prvSold else newPrice


def publishMessage(message, basic=True):
    if __debug__:
        logging.info(f'DEBUGGED (Basic={basic}): {message}')
    else:
        if basic:
            try:
                twitterApi.update_status(message[:280])
            except:
                logging.exception("Exception when tweeting:")

            try:
                result = tg.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=message
                )
                result.wait()
            except:
                logging.exception("Exception while basic telegramming:")


def getPool(pdePoolPairs, hBeacon, coinOnRadar, intermediateCoin=PRV_ID):
    poolId = f'pdepool-{hBeacon}-{intermediateCoin}-{coinOnRadar}'

    return pdePoolPairs[poolId] if poolId in pdePoolPairs and pdePoolPairs[poolId]["Token1PoolValue"] > 0 and \
                                   pdePoolPairs[poolId]["Token2PoolValue"] > 0 else None


def hasEnoughLiquidity(coinOnRadar, pdePoolPairs, hBeacon):
    prvUsdtPair = pdePoolPairs[f'pdepool-{hBeacon}-{PRV_ID}-{USDT_ID}']
    prvUsdcPair = pdePoolPairs[f'pdepool-{hBeacon}-{PRV_ID}-{USDC_ID}']

    usdtPrecision = getCoin(USDT_ID)['PDecimals']
    usdcPrecision = getCoin(USDC_ID)['PDecimals']
    prvPrecision = getCoin(PRV_ID)['PDecimals']

    prvPrice = (coin(prvUsdtPair['Token2PoolValue'], usdtPrecision, False) + coin(prvUsdcPair['Token2PoolValue'],
                                                                                  usdcPrecision, False)) / coin(
        prvUsdtPair['Token1PoolValue'] + prvUsdcPair['Token1PoolValue'], prvPrecision, False)

    liquidity = coin(pdePoolPairs[f'pdepool-{hBeacon}-{PRV_ID}-{coinOnRadar}']['Token1PoolValue'],
                     prvPrecision, False) * prvPrice * 2

    logging.info(f'Liquidity of PRV/{getCoin(coinOnRadar)["Symbol"]} = {liquidity}')

    return liquidity >= 10000


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


def getCoin(tokenId):
    global tokenDict, publicApi

    if tokenId not in tokenDict:
        result = publicApi.get_privacy_custom_token(tokenId)
        if result.get_result("IsExist"):
            propCoin = result.get_result("CustomToken")
            tokenDict.update({tokenId: {'Symbol': propCoin['Symbol'], 'PSymbol': propCoin['Symbol'], 'PDecimals': 9}})
        else:
            publicApi.refresh_token_list()
            tokenDict.update(publicApi.TokenDict)

    return tokenDict[tokenId] if tokenId in tokenDict else None


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
beaconHeight = -1
numTotalStaker = -1
LOWEST_GAIN_LIMIT = 1

recentPdeStates = deque(maxlen=5)
recentPdeTrades = deque(maxlen=5)

trackAllTxs()
