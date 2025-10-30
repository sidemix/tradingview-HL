@app.route('/webhook', methods=['POST'])
def handle_webhook():
    try:
        # Get raw data first for debugging
        raw_data = request.get_data(as_text=True)
        print(f"Raw data received: {raw_data}")
        
        if not raw_data or raw_data.strip() == '':
            return jsonify({"error": "Empty request body"}), 400
        
        # Try to parse JSON
        try:
            data = request.get_json()
        except Exception as e:
            print(f"JSON parse error: {str(e)}")
            return jsonify({"error": f"Invalid JSON: {str(e)}"}), 400
        
        if data is None:
            return jsonify({"error": "No JSON data received"}), 400
        
        print(f"Parsed data: {data}")
        
        # Parse the trading alert with defaults
        symbol = data.get('symbol', 'BTC').upper()
        side = data.get('side', '').lower()
        size = float(data.get('size', 0.01))
        order_type = data.get('order_type', 'market')
        
        print(f"Processing order: {side} {size} {symbol}")
        
        # Get available coins to find correct symbol
        available_coins = hl.get_available_coins()
        print(f"Available coins: {available_coins}")
        
        # Find the correct coin name
        target_coin = None
        for coin in available_coins:
            if symbol in coin.upper():
                target_coin = coin
                break
        
        if not target_coin:
            target_coin = symbol  # Fallback to original
        
        print(f"Using coin: {target_coin}")
        
        # Execute trade on Hyperliquid
        if side in ['buy', 'sell']:
            result = hl.order(target_coin, side == 'buy', size, order_type)
            print(f"Hyperliquid response: {result}")
            
            if result.get('status') == 'success':
                return jsonify({
                    "status": "success", 
                    "message": f"Order executed: {side} {size} {target_coin}",
                    "details": result.get('response', {})
                })
            else:
                error_msg = result.get('error', 'Unknown error from Hyperliquid')
                return jsonify({
                    "status": "error",
                    "message": error_msg
                }), 400
        else:
            return jsonify({"error": "Invalid side. Use 'buy' or 'sell'"}), 400
            
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return jsonify({"error": str(e)}), 500
