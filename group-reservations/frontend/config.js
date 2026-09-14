// Public runtime configuration. Localhost uses the reloadable local API;
// deployed frontends continue to use the ECS API.
const localFrontend = ["localhost", "127.0.0.1"].includes(window.location.hostname);
window.WAE_API_BASE = localFrontend
  ? "http://127.0.0.1:8000"
  : "https://wh-7e0f2f4e396c48eda8e92a367d77cb2f.ecs.us-west-2.on.aws";
