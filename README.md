# Blogcast

Articles you choose, plus new posts from NutritionFacts.org and Decent Espresso, in one podcast feed.
YouTube submissions use the video's original audio. They receive no narration, summary, or added intro.

**Subscribe:** https://omerr.github.io/blogcast/feed.xml

Send a public article or YouTube video URL as the entire message body to your ntfy submission topic.
The topic lives in the `NTFY_SUBMISSION_TOPIC` repository secret.
Anyone who knows the topic can submit requests. Submitted URLs and episodes are public.

```sh
curl --fail --data 'https://www.youtube.com/watch?v=VIDEO_ID' https://ntfy.sh/YOUR_TOPIC
```

GitHub Actions checks the inbox hourly, at minute 17. GitHub may delay scheduled runs.
NutritionFacts and Decent Espresso are checked once per UTC day.
The workflow can also be started manually from the Actions tab.

The `automation` branch holds the code and workflow. The `state` branch stores the episode ledger, audio, and request queue.
State is saved before deployment. Requests are acknowledged and notifications sent after GitHub Pages deploys successfully.
The ntfy server keeps unread messages for about 12 hours; an extended GitHub outage can outlast that window.

YouTube currently blocks unauthenticated downloads from the tested GitHub runners.
Original-audio import is implemented, but cloud requests need a working authenticated download or proxy setup.
Blocked videos remain retryable and produce an error notification after five attempts. They never fall back to narration.
PDFs and pages requiring login are unsupported.

## Development

```sh
pip install -e '.[extract,media]' pytest
python -m pytest -q
```

Audio extraction requires FFmpeg and Node.js. The workflow provides both.
The narration engine is included in this repository so deployments need no access to another private repository.
