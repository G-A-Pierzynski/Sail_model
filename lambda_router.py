import json
import boto3
import base64
from urllib.parse import parse_qs

# Lambda client to invoke other functions
lambda_client = boto3.client('lambda')

def lambda_handler(event, context):
    """
    Smart router that directs SMS to appropriate Lambda function
    based on message content
    """
    try:
        # Parse Twilio webhook data
        if event.get('isBase64Encoded'):
            body = base64.b64decode(event['body']).decode('utf-8')
        else:
            body = event.get('body', '')
        
        data = parse_qs(body)
        sms_body = data.get('Body', [''])[0]
        from_number = data.get('From', [''])[0]
        
        print(f"Received SMS from {from_number}: {sms_body}")
        
        # Determine which Lambda to invoke based on message content
        if '->' in sms_body:
            # Route request (has arrow)
            target_function = 'route-sms-service'
            print("Routing to route service")
        else:
            # Weather request (simple coordinates or commands)
            target_function = 'weather-sms-service'
            print("Routing to weather service")
        
        # Invoke the appropriate Lambda function
        response = lambda_client.invoke(
            FunctionName=target_function,
            InvocationType='RequestResponse',
            Payload=json.dumps(event)
        )
        
        # Read and return the response
        response_payload = json.loads(response['Payload'].read())
        print(f"Response from {target_function}: {response_payload}")
        
        return response_payload
        
    except Exception as e:
        print(f"Router error: {e}")
        # Return empty TwiML response on error
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/xml',
                'Access-Control-Allow-Origin': '*'
            },
            'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        }