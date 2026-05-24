import os
import sys


genai = None


def check_gemini_api_key(api_key):
    global genai
    if genai is None:
        from google import genai as google_genai
        genai = google_genai

    client = genai.Client(api_key=api_key)
    try:
        client.models.list()
    except Exception as e:
        print(f"Error: {e}")
        return False
    else:
        return True


if __name__ == "__main__":  # pragma: no cover
    api_key = None
    if len(sys.argv) > 1:
        api_key = sys.argv[1]
    else:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Usage: python test_google_key.py <GEMINI_API_KEY> or set GEMINI_API_KEY env var.")
        sys.exit(1)
    if check_gemini_api_key(api_key):
        print("Valid Gemini API key.")
    else:
        print("Invalid Gemini API key.")
