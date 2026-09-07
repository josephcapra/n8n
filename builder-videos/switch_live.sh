#!/usr/bin/env bash
# Verify the finder-v2 tagged revision on the production service serves the published page correctly,
# then route 100% of public traffic to it. Rollback: gcloud run services update-traffic paradise-finder --region us-east1 --to-revisions paradise-finder-00030-wpg=100
set -u
P=paradise-automation; R=us-east1
T=https://finder-v2---paradise-finder-3vuuwnsvua-ue.a.run.app
PUB=https://paradise-finder-3vuuwnsvua-ue.a.run.app
code=$(curl -s -o /tmp/v2.html -w "%{http_code}" "$T/?verify=$(date +%s)")
robots=$(grep -c 'name="robots"' /tmp/v2.html || true); canon=$(grep -c 'rel="canonical"' /tmp/v2.html || true); click=$(grep -c 'data-href=' /tmp/v2.html || true)
data=$(curl -s -o /dev/null -H "Accept-Encoding: gzip" -w "%{http_code}" "$T/communities_all.js")
echo "finder-v2: page=$code robots_meta=$robots canonical=$canon clickable=$click data=$data"
if [ "$code" = "200" ] && [ "$robots" = "0" ] && [ "$canon" = "1" ] && [ "$click" = "1" ] && [ "$data" = "200" ]; then
  gcloud run services update-traffic paradise-finder --region $R --project $P --to-tags finder-v2=100 --quiet 2>&1 | grep -E "finder-v2|percent|ERROR" | head -3
  sleep 5
  curl -s -o /tmp/pub.html -w "public page: %{http_code}\n" "$PUB/?verify=$(date +%s)"
  grep -o '<title>[^<]*' /tmp/pub.html; echo "new finder markup on public URL: $(grep -c 'data-href=' /tmp/pub.html)"
  echo "traffic now:"; gcloud run services describe paradise-finder --region $R --project $P --format="value(status.traffic)" | tr ';' '\n' | grep -E "percent"
else
  echo "NOT SWITCHING — checks failed"; exit 1
fi
