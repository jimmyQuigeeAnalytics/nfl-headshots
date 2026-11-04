import os
import requests

# 1. Configuration
# Create a dictionary of players you want to download
# Format: "Player_Name": "ESPN_Player_ID"
PLAYERS_TO_DOWNLOAD = {
    "Patrick_Mahomes": "3139477",
    "Lamar_Jackson": "3916387",
    "Justin_Jefferson": "4262921",
    "Christian_McCaffrey": "3117251",
    "Josh_Allen": "3918298"
}

# Folder where your transparent headshots will be saved
OUTPUT_FOLDER = "nfl_headshots"

# 2. Main Download Loop
def download_nfl_headshots():
    # Create the output folder if it doesn't already exist
    if not os.path.exists(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)
        print(f"Created folder: {OUTPUT_FOLDER}")

    print(f"Starting download of {len(PLAYERS_TO_DOWNLOAD)} player headshots...\n")

    for player_name, player_id in PLAYERS_TO_DOWNLOAD.items():
        # Construct the official ESPN CDN high-res transparent URL
        cdn_url = f"https://a.espncdn.com/i/headshots/nfl/players/full/{player_id}.png"

        # Define local file destination path
        destination_path = os.path.join(OUTPUT_FOLDER, f"{player_name}.png")

        try:
            # Request the image from the CDN
            response = requests.get(cdn_url, timeout=10)

            # Check if the player ID actually exists (HTTP Status 200)
            if response.status_code == 200:
                with open(destination_path, 'wb') as file:
                    file.write(response.content)
                print(f"✅ Successfully downloaded: {player_name}.png")
            else:
                print(f"❌ Failed: Could not find image for ID {player_id} ({player_name}). HTTP Status: {response.status_code}")

        except requests.exceptions.RequestException as e:
            print(f"⚠️ Network error downloading {player_name}: {e}")

    print("\n🎉 Headshot download task completed!")

if __name__ == "__main__":
    download_nfl_headshots()
