-- Normalize allowlisted source records; never read application configuration.
local services = {}
local loaded_at = 0

local function redact(value)
    value = string.gsub(value, "https?://%S+", "[url]")
    value = string.gsub(value, "[Aa]uthorization[%s:=\"]+[^,\n]+", "Authorization=[redacted]")
    value = string.gsub(value, "[Pp]assword[%s:=\"]+[^,\n]+", "password=[redacted]")
    value = string.gsub(value, "[Tt]oken[%s:=\"]+[^,\n]+", "token=[redacted]")
    value = string.gsub(value, "[Aa][Pp][Ii][Kk]ey[%s:=\"]+[^,\n]+", "ApiKey=[redacted]")
    value = string.gsub(value, "[Cc]ookie[%s:=\"]+[^,\n]+", "cookie=[redacted]")
    value = string.gsub(value, "/media/[^%s\"]+", "[private-path]")
    value = string.gsub(value, "/home/[^%s\"]+", "[private-path]")
    return string.sub(value, 1, 16000)
end

function normalize(tag, timestamp, record)
    if os.time() - loaded_at > 60 then
        local file = io.open("/etc/homelab-monitor/containers.map", "r")
        if file then
            services = {}
            for line in file:lines() do
                local id, name = string.match(line, "^(%w+)|(.+)$")
                if id then services[id] = name end
            end
            file:close()
        end
        loaded_at = os.time()
    end
    local message = record["message"] or record["log"] or record["MESSAGE"] or ""
    local file = record["file"] or ""
    local id = string.match(file, "/containers/(%w+)/")
    if id and not services[id] then return -1, timestamp, record end
    local service = record["service"] or record["_SYSTEMD_UNIT"] or services[id] or tag
    if tag == "logs.unmanic" then service = "recursing_ryan-main_app-1" end
    if tag == "logs.jellyfin" then service = "jellyfin" end
    local level = record["level"]
    local lower = string.lower(tostring(message))
    local declared = string.match(lower, '"level"%s*:%s*"(%a+)"')
    if not level and (declared == "trace" or declared == "debug" or declared == "info"
            or declared == "warn" or declared == "warning" or declared == "error"
            or declared == "critical" or declared == "fatal") then
        level = declared == "warn" and "warning" or declared
    end
    -- Successful JSON access logs commonly include an empty error field.
    -- That field is not evidence of an application failure.
    local empty_error = string.find(lower, '"error"%s*:%s*""')
    local status = tonumber(string.match(lower, '"status"%s*:%s*(%d+)'))
    if not level and empty_error and status and status >= 200 and status < 400 then
        level = "info"
    end
    lower = string.gsub(lower, '"error"%s*:%s*""', '')
    lower = string.gsub(lower, '"error"%s*:%s*null', '')
    if not level then
        local priority = tonumber(record["PRIORITY"])
        if priority and priority <= 2 then level = "critical"
        elseif priority == 3 then level = "error"
        elseif priority == 4 then level = "warning"
        elseif string.find(lower, "fatal") or string.find(lower, "panic") then level = "fatal"
        elseif string.find(lower, "error") or string.find(lower, "traceback") or string.find(lower, "%[err%]") then level = "error"
        elseif string.find(lower, "warn") then level = "warning"
        else level = "info" end
    end
    local result = {host="thinkserver", service=service, level=string.lower(level),
                    source_type=id and "docker" or (record["MESSAGE"] and "journal" or "file"),
                    message=redact(tostring(message))}
    if id then result["container_id"] = id end
    if record["stream"] then result["stream"] = record["stream"] end
    -- Prefer Docker's original timestamp for ordinary logs, not replay arrival time.
    if record["time"] then result["_timestamp"] = record["time"] end
    return 1, timestamp, result
end
