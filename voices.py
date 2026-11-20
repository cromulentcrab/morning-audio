"""Email short samples of several Australian voices, so you can pick one.

Run it from GitHub: Actions -> "Voice samples" -> Run workflow.
Then set the one you like as a repository variable called VOICE
(Settings -> Secrets and variables -> Actions -> Variables).
"""
import os
import smtplib
import ssl
import subprocess
import tempfile
import wave
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import briefing as b

VOICES = {
    "en-AU-Chirp3-HD-Aoede": "female",
    "en-AU-Chirp3-HD-Kore": "female",
    "en-AU-Chirp3-HD-Leda": "female",
    "en-AU-Chirp3-HD-Sulafat": "female",
    "en-AU-Chirp3-HD-Charon": "male",
    "en-AU-Chirp3-HD-Iapetus": "male",
    "en-AU-Chirp3-HD-Orus": "male",
    "en-AU-Chirp3-HD-Schedar": "male",
}

SAMPLE = ("Good morning, Antoinette. It's Saturday, the twenty-first of November. "
          "The Guardian reports that the federal government will introduce its revised nature "
          "laws to parliament next week, after months of negotiations with the Greens and the "
          "crossbench. Writing in Pearls and Irritations, a former senior public servant argues "
          "the changes don't go far enough. Now to the environment. RenewEconomy says South "
          "Australia ran on wind and solar alone for most of yesterday afternoon.")


def main():
    files = []
    tmp = Path(tempfile.mkdtemp())
    for name, kind in VOICES.items():
        try:
            pcm = b.synthesize(SAMPLE, voice=name)
        except Exception as e:  # noqa: BLE001
            print(f"{name}: {e}")
            continue
        b._voice_in_use = None
        wav, mp3 = tmp / f"{name}.wav", tmp / f"{name} ({kind}).mp3"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-b:a", "64k", str(mp3)], check=True)
        files.append(mp3)
        print(f"{name}: ok")
    if not files:
        raise SystemExit("No voices worked; check the GOOGLE_TTS_KEY secret")

    sender = os.environ["EMAIL_FROM"]
    msg = MIMEMultipart()
    msg["Subject"] = f"{b.SHOW_NAME}: voice samples"
    msg["From"] = sender
    msg["To"] = sender
    msg.attach(MIMEText(
        "Here are some voices for the show. Each file is named after the voice.\n\n"
        "To choose one: in the GitHub repository go to Settings > Secrets and variables > "
        "Actions > Variables, and add a variable called VOICE with the voice's name, "
        "for example en-AU-Chirp3-HD-Aoede (without the bit in brackets).", "plain"))
    for f in files:
        part = MIMEApplication(f.read_bytes(), "mpeg")
        part.add_header("Content-Disposition", "attachment", filename=f.name)
        msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, os.environ["APP_PASSWORD"].replace(" ", ""))
        s.sendmail(sender, [sender], msg.as_string())
    print(f"Emailed {len(files)} samples")


if __name__ == "__main__":
    main()
