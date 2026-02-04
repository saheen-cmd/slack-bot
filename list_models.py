import google.genai as genai

# Use your API key directly for testing
client = genai.Client(api_key="AIzaSyCnerc0xx8rc8fuwBAVeGhOhXXScSTDQwE")

models = client.models.list()
for m in models:
    print(m.name)
