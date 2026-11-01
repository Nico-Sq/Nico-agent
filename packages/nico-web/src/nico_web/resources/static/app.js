async function bootstrap() {
  try {
    const config = await api("/config");
    nicoState.modelReady = !config.config_error;
    $("provider").textContent = config.config_error ? "模型未就绪" : config.provider === "demo" ? "演示模式" : `模型：${config.model}`;
    $("provider-data-notice").hidden = config.provider === "demo";
    $("model-config-notice").hidden = nicoState.modelReady;
    $("model-config-notice").textContent = config.config_error ? `${config.config_error}；修改环境变量后重启应用。` : "";
    setBusy("chat", false);
    setBusy("interview", false);
    $("logout").hidden = !config.auth_required;
    if (config.auth_required) {
      try {
        const user = await api("/auth/me");
        showApp(user.username);
      } catch { showAuth(); return; }
    } else showApp();
    await loadModels();
    await switchKind("chat");
  } catch (error) {
    showAuth();
    $("auth-notice").textContent = error.message;
  }
}
bootstrap();
