# Scheduled ingestion script for heliophysics papers.
# Designed to be run by cron. Pulls latest papers from arXiv daily
# and the last month from ADS weekly.
#
# To install, run: crontab -e
# Then add these two lines:
#   0 6 * * * /home/oana-vesa/Documents/research-api/scheduled_ingest.sh arxiv
#   0 7 * * 1 /home/oana-vesa/Documents/research-api/scheduled_ingest.sh ads

set -eou pipefail

PROJECT_DIR="$HOME/Documents/research-api"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

SOURCE="${1:-arxiv}"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOGFILE="$LOG_DIR/ingest_${SOURCE}_${TIMESTAMP}.log"

echo "[$TIMESTAMP] Starting scheduled ingestion: source=$SOURCE" | tee -a "$LOGFILE"

cd "$PROJECT_DIR"
source venv/bin/activate

if [ "$SOURCE" = "arxiv" ]; then
    python ingest.py --source arxiv --max 200 2>&1 | tee -a "$LOGFILE"

elif [ "$SOURCE" = "ads" ]; then
    START=$(date -d "30 days ago" +"%Y-%m")
    END=$(date +"%Y-%m")
    python ingest.py --source ads --start "$START" --end "$END" --max 200 --keywords "rossby wave,inertial wave,rossby mode,inertial mode" 2>&1 | tee -a "$LOGFILE"
else
    echo "Unknown source: $SOURCE. Use 'arxiv' or 'ads'." | tee -a "$LOGFILE"
    exit 1
fi

echo "[$(date +"%Y-%m-%d_%H-%M-%S")] Ingestion complete." | tee -a "$LOGFILE"
