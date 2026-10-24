function main() {
  var fieldName = "home_media_queue";
  var kinds = [
    { plural: "images", find: "findImages", one: "findImage", update: "imageUpdate", input: "ImageUpdateInput" },
    { plural: "scenes", find: "findScenes", one: "findScene", update: "sceneUpdate", input: "SceneUpdateInput" }
  ];
  var mode = input && input.Args && input.Args.mode ? input.Args.mode : "status";
  var counts = { queued: 0, cancelled: 0, skippedTagged: 0, skippedActive: 0, errors: 0 };
  var statuses = { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0, unqueued: 0 };

  function parseObject(value) {
    if (!value) return {};
    if (typeof value === "string") {
      try { return JSON.parse(value); } catch (_) { return {}; }
    }
    return typeof value === "object" ? value : {};
  }

  function getCustomFields(item) {
    return parseObject(item && item.custom_fields);
  }

  function getRequest(item) {
    return parseObject(getCustomFields(item)[fieldName]);
  }

  function isActive(request) {
    return request.status === "queued" || request.status === "running";
  }

  function hasProvenance(item) {
    var fields = getCustomFields(item);
    var completed = parseObject(fields.home_media_auto);
    // Existing running-batch manifests were also written only with the final tag update.
    // Explicit processing/interrupted markers always override an older completed result.
    if (completed.completion_state !== undefined && completed.completion_state !== "complete") {
      return false;
    }
    return typeof completed.model === "string" && completed.model.length > 0 &&
      typeof completed.hash === "string" && completed.hash.length > 0 &&
      completed.score_format === "probability-v1" &&
      Array.isArray(completed.sample_times) && completed.sample_times.length > 0;
  }

  function queryItems(kind, page) {
    var query = "query($p:Int!){" + kind.find +
      "(filter:{page:$p,per_page:100,sort:\"id\",direction:ASC}){" +
      "count " + kind.plural + "{id custom_fields}}}";
    return gql.Do(query, { p: page })[kind.find];
  }

  function allItems(kind) {
    var result = [];
    var page = 1;
    var response;
    do {
      response = queryItems(kind, page);
      result = result.concat(response[kind.plural] || []);
      page += 1;
    } while (result.length < response.count);
    return result;
  }

  function getLatest(kind, id) {
    var query = "query($id:ID!){" + kind.one + "(id:$id){id custom_fields}}";
    return gql.Do(query, { id: id })[kind.one];
  }

  function writeRequest(kind, item, request) {
    var mutation = "mutation($i:" + kind.input + "!){" + kind.update +
      "(input:$i){id}}";
    var variables = { i: {
      id: item.id,
      custom_fields: { partial: {} }
    } };
    variables.i.custom_fields.partial[fieldName] = JSON.stringify(request);
    gql.Do(mutation, variables);
  }

  function newRequest() {
    var now = new Date();
    return {
      request_id: now.getTime().toString(36) + "-" +
        Math.floor(Math.random() * 16777216).toString(36) +
        Math.floor(Math.random() * 16777216).toString(36),
      status: "queued",
      requested_at: now.toISOString(),
      updated_at: now.toISOString()
    };
  }

  function queueItem(kind, item, force) {
    var original = getRequest(item);
    if (isActive(original)) {
      counts.skippedActive += 1;
      return;
    }
    if (!force && original.status !== "failed" && hasProvenance(item)) {
      counts.skippedTagged += 1;
      return;
    }
    var latest = getLatest(kind, item.id);
    if (!latest) {
      counts.errors += 1;
      return;
    }
    var current = getRequest(latest);
    if (isActive(current)) {
      counts.skippedActive += 1;
      return;
    }
    if (!force && current.status !== "failed" && hasProvenance(latest)) {
      counts.skippedTagged += 1;
      return;
    }
    try {
      writeRequest(kind, latest, newRequest());
      counts.queued += 1;
    } catch (_) {
      counts.errors += 1;
    }
  }

  function cancelItem(kind, item) {
    var latest = getLatest(kind, item.id);
    if (!latest) {
      counts.errors += 1;
      return;
    }
    var current = getRequest(latest);
    if (current.status !== "queued") return;
    current.status = "cancelled";
    current.updated_at = new Date().toISOString();
    try {
      writeRequest(kind, latest, current);
      counts.cancelled += 1;
    } catch (_) {
      counts.errors += 1;
    }
  }

  function statusItem(item) {
    var request = getRequest(item);
    if (Object.prototype.hasOwnProperty.call(statuses, request.status)) {
      statuses[request.status] += 1;
    } else {
      statuses.unqueued += 1;
    }
  }

  if (["pending", "all", "cancel", "status"].indexOf(mode) === -1) {
    return { Output: "Unsupported queue mode." };
  }

  for (var i = 0; i < kinds.length; i += 1) {
    var kind = kinds[i];
    var items = allItems(kind);
    for (var j = 0; j < items.length; j += 1) {
      var item = items[j];
      if (mode === "status") {
        statusItem(item);
      } else if (mode === "cancel") {
        cancelItem(kind, item);
      } else if (mode === "all") {
        queueItem(kind, item, true);
      } else {
        queueItem(kind, item, false);
      }
    }
  }

  var summary;
  if (mode === "status") {
    summary = "Tagging queue status: " + JSON.stringify(statuses);
  } else if (mode === "cancel") {
    summary = "Cancelled " + counts.cancelled + " queued requests; running requests continue. " +
      counts.errors + " errors.";
  } else {
    summary = "Queued " + counts.queued + " requests; skipped " + counts.skippedTagged +
      " already tagged and " + counts.skippedActive + " active requests; " + counts.errors +
      " errors.";
  }
  log.Info(summary);
  return { Output: summary };
}

main();
