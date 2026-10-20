import json
import time
import requests
from openai import OpenAI

with open("config.json", "r", encoding="utf-8-sig") as f:
    config = json.load(f)

client = OpenAI(
    base_url=config["base_url"],
    api_key=config["api_key"]
)

def generate_image(prompt=None):
    prompt = prompt or config["default_image_prompt"]
    r = client.images.generate(
        model=config["image_model"],
        prompt=prompt
    )
    url = r.data[0].url
    print(f"Image URL: {url}")
    return url

def generate_video(prompt=None, duration=None):
    prompt = prompt or config["default_video_prompt"]
    duration = duration or config["video_duration"]
    base_url = config["base_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    
    t = requests.post(
        f"{base_url}/videos",
        json={"prompt": prompt, "duration": duration},
        headers=headers
    ).json()
    
    video_id = t.get("id")
    print(f"Video task created, ID: {video_id}")
    
    while True:
        s = requests.get(
            f"{base_url}/videos/{video_id}",
            headers=headers
        ).json()
        status = s.get("status")
        print(f"Video status: {status}")
        if status in ("succeeded", "failed", "completed"):
            break
        time.sleep(config["poll_interval"])
        
    print(s)
    return s

if __name__ == "__main__":
    print("Generating image...")
    generate_image()
    print("\nGenerating video...")
    generate_video()

