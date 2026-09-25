# Login shells reset Docker ENV PATH. Restore the collection entry point.
case "$PATH" in
  /opt/chart2code/bin:*) ;;
  *) export PATH="/opt/chart2code/bin:$PATH" ;;
esac
