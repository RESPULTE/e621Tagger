-- Send only relevant Windows event metadata during the approved run.
function platform_event(tag, timestamp, record)
    local provider = record["ProviderName"] or record["SourceName"] or "Windows"
    local event_id = record["EventID"] or record["EventId"] or 0
    local level = tonumber(record["Level"]) or 4
    return 1, timestamp, {
        host="tagging-laptop", service="hydra", source_type="windows_event",
        event_type="platform_evidence", run_id=os.getenv("HOME_MEDIA_RUN_ID"),
        source_epoch=os.time(), level=(level <= 2) and "error" or "info",
        message=tostring(provider) .. ": event " .. tostring(event_id),
        provider=tostring(provider), windows_event_id=tonumber(event_id),
        channel=record["Channel"] or record["LogName"]
    }
end
