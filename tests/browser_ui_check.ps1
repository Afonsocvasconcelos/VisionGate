$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$checkRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("visiongate-browser-" + [guid]::NewGuid().ToString("N"))
$edgePath = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

function Free-Port {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start() | Out-Null
    $port = $listener.LocalEndpoint.Port
    $listener.Stop() | Out-Null
    return $port
}

function Invoke-Cdp([string]$Method, [hashtable]$Params = @{}) {
    $script:cdpId++
    $id = $script:cdpId
    $json = @{id = $id; method = $Method; params = $Params} | ConvertTo-Json -Compress -Depth 12
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $socket.SendAsync(
        [ArraySegment[byte]]::new($bytes),
        [Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [Threading.CancellationToken]::None
    ).Wait()
    while ($true) {
        $buffer = New-Object byte[] 262144
        $segment = [ArraySegment[byte]]::new($buffer)
        $received = $socket.ReceiveAsync($segment, [Threading.CancellationToken]::None).Result
        $message = [Text.Encoding]::UTF8.GetString($buffer, 0, $received.Count)
        while (-not $received.EndOfMessage) {
            $received = $socket.ReceiveAsync($segment, [Threading.CancellationToken]::None).Result
            $message += [Text.Encoding]::UTF8.GetString($buffer, 0, $received.Count)
        }
        $payload = $message | ConvertFrom-Json
        if ($payload.id -eq $id) {
            if ($payload.error) { throw ($payload.error | ConvertTo-Json -Compress) }
            return $payload.result
        }
    }
}

function Evaluate([string]$Expression, [bool]$Await = $false) {
    return Invoke-Cdp "Runtime.evaluate" @{
        expression = $Expression
        awaitPromise = $Await
        returnByValue = $true
    }
}

New-Item -ItemType Directory -Path $checkRoot | Out-Null
$server = $null
$edge = $null
$socket = $null
try {
    if (-not (Test-Path -LiteralPath $edgePath)) { throw "Microsoft Edge is not installed" }
    $serverPort = Free-Port
    $debugPort = Free-Port
    $passwordHash = & "$root\.venv\Scripts\python.exe" -c "from auth import hash_password; print(hash_password('browser-check', n=1024))"
    $env:DATA_DIR = Join-Path $checkRoot "data"
    $env:DISABLE_VISION = "1"
    $env:VISIONGATE_USERNAME = "browser"
    $env:VISIONGATE_PASSWORD_HASH = $passwordHash
    $env:TRUSTED_HOSTS = "127.0.0.1,localhost"
    $server = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
        -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", $serverPort, "--no-proxy-headers") `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $checkRoot "server.log") `
        -RedirectStandardError (Join-Path $checkRoot "server-error.log") -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$serverPort/login" -TimeoutSec 1).StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw "Browser-check server did not start" }

    $edge = Start-Process -FilePath $edgePath -ArgumentList @(
        "--headless", "--disable-gpu", "--no-first-run",
        "--remote-debugging-port=$debugPort", "--user-data-dir=$(Join-Path $checkRoot 'edge')",
        "--window-size=1440,900", "http://127.0.0.1:$serverPort/login"
    ) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $checkRoot "edge.log") `
        -RedirectStandardError (Join-Path $checkRoot "edge-error.log") -PassThru
    $target = $null
    $debugFailure = ""
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$debugPort/json" -TimeoutSec 1
            $target = @($targets | Where-Object { $_.type -eq "page" })[0]
            if ($target) { break }
        } catch { $debugFailure = $_.Exception.Message }
        Start-Sleep -Milliseconds 250
    }
    if (-not $target) {
        $edgeError = Get-Content -LiteralPath (Join-Path $checkRoot "edge-error.log") -Raw -ErrorAction SilentlyContinue
        throw "Edge debugging endpoint did not start on $debugPort (exited=$($edge.HasExited), request=$debugFailure): $edgeError"
    }

    $socket = [Net.WebSockets.ClientWebSocket]::new()
    $socket.ConnectAsync([Uri]$target.webSocketDebuggerUrl, [Threading.CancellationToken]::None).Wait()
    $script:cdpId = 0
    Invoke-Cdp "Runtime.enable" | Out-Null
    Evaluate @'
fetch('/api/auth/login', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({username: 'browser', password: 'browser-check'})
}).then(async response => {
  if (!response.ok) throw Error(response.status);
  const session = await response.json();
  const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token};
  const cameraResponse = await fetch('/api/cameras', {
    method: 'POST', headers,
    body: JSON.stringify({name:'Browser camera',stream_url:'rtsp://camera.local:554/live',username:'',password:'',enabled:false})
  });
  if (!cameraResponse.ok) throw Error(`camera ${cameraResponse.status}`);
  const camera = await cameraResponse.json();
  const graph = (name, trigger) => ({
    schema_version:1,name,enabled:true,revision:1,max_concurrent_runs:1,
    nodes:[trigger,{id:'camera',kind:'action.camera.disable',config:{camera_id:camera.id},position:{x:380,y:120}}],
    edges:[{id:'edge',from:trigger.id,to:'camera',from_port:'right',to_port:'left',outcome:'success'}]
  });
  const eventGraph = graph('Camera events',{id:'event',kind:'trigger.camera.connection',config:{camera_id:camera.id,online:true},position:{x:80,y:120}});
  const manualGraph = graph('Manual camera',{id:'manual',kind:'trigger.manual',config:{},position:{x:80,y:120}});
  for (const [name, document] of [['Camera events',eventGraph],['Manual camera',manualGraph]]) {
    const created = await fetch('/api/automations',{method:'POST',headers,body:JSON.stringify({name,enabled:name !== 'Manual camera',graph:document})});
    if (!created.ok) throw Error(`automation ${created.status}`);
  }
  setTimeout(() => location.href = '/', 0);
})
'@ $true | Out-Null
    Start-Sleep -Milliseconds 500
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $state = Evaluate "({path:location.pathname,ready:document.readyState,hasPicker:!!document.getElementById('dashboardAutomation')})"
        if ($state.result.value.path -eq "/" -and $state.result.value.hasPicker) { break }
        Start-Sleep -Milliseconds 200
    }
    $dashboard = Evaluate @'
(async () => {
  const select = document.getElementById('dashboardAutomation');
  const waitForModules = async expected => {
    for (let attempt=0; attempt<30; attempt++) {
      const modules = (await (await fetch('/api/dashboard/automation')).json()).modules;
      if (JSON.stringify(modules) === JSON.stringify(expected)) return modules;
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    throw Error(`dashboard modules were not saved: ${expected}`);
  };
  const beforeManual = !!document.querySelector('.manual-module');
  select.value = [...select.options].find(option => option.textContent === 'Manual camera').value;
  select.dispatchEvent(new Event('change', {bubbles:true}));
  for (let attempt=0; attempt<30 && !document.querySelector('.manual-module'); attempt++) await new Promise(resolve=>setTimeout(resolve,100));
  const selectedModules = [...document.querySelectorAll('[data-module-id]')].map(item=>item.dataset.moduleId);
  document.getElementById('customizeDashboard').click();
  const editControls = document.querySelectorAll('.module-edit-controls').length;
  [...document.querySelectorAll('.module-edit-controls button')].find(button=>button.title.startsWith('Remove Browser camera')).click();
  for (let attempt=0; attempt<20 && document.querySelectorAll('[data-module-id]').length!==1; attempt++) await new Promise(resolve=>setTimeout(resolve,50));
  const afterRemove = document.querySelectorAll('[data-module-id]').length;
  await waitForModules(['manual']);
  document.getElementById('addDashboardModule').click();
  for (let attempt=0; attempt<20 && document.querySelectorAll('[data-module-id]').length!==2; attempt++) await new Promise(resolve=>setTimeout(resolve,50));
  const afterAdd = document.querySelectorAll('[data-module-id]').length;
  const added = [...document.querySelectorAll('[data-module-id]')].map(item=>item.dataset.moduleId);
  await waitForModules(added);
  document.querySelector('[data-module-id="manual"] .module-edit-controls button[title="Move later"]').click();
  for (let attempt=0; attempt<20 && document.querySelector('[data-module-id]')?.dataset.moduleId==='manual'; attempt++) await new Promise(resolve=>setTimeout(resolve,50));
  const reordered = [...document.querySelectorAll('[data-module-id]')].map(item=>item.dataset.moduleId);
  const persisted = await waitForModules(reordered);
  return {
    width:innerWidth,
    scroll:document.documentElement.scrollWidth,
    hasPicker:!!select,
    hasTrash:!!document.querySelector('[data-camera-delete]'),
    noPrimary:!document.body.innerText.includes('Primary Door'),
    beforeManual,
    selectedModules,
    editControls,
    afterRemove,
    afterAdd,
    reordered,
    persisted,
    runButton:[...document.querySelectorAll('.manual-actions button')].some(button=>button.textContent==='Run now')
  };
})()
'@ $true

    Evaluate "window.confirm=()=>true; [...document.querySelectorAll('.manual-actions button')].find(button=>button.textContent==='Run now').click(); true" | Out-Null
    $manualRun = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $manualRun = Evaluate "document.querySelector('.manual-status')?.textContent || ''"
        if ($manualRun.result.value.Contains('completed')) { break }
        Start-Sleep -Milliseconds 100
    }

    Evaluate "setTimeout(()=>location.href='/automations',0)" | Out-Null
    Start-Sleep -Milliseconds 500
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $loaded = Evaluate "document.querySelectorAll('.graph-node').length"
        if ($loaded.result.value -ge 2) { break }
        Start-Sleep -Milliseconds 200
    }
    $desktop = Evaluate @'
(() => {
  const template = document.querySelector('.node-template.condition');
  const canvas = document.getElementById('graphCanvas');
  const transfer = new DataTransfer();
  template.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer:transfer}));
  canvas.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, clientX:620, clientY:350, dataTransfer:transfer}));
  canvas.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, clientX:620, clientY:350, dataTransfer:transfer}));
  const condition = [...document.querySelectorAll('.graph-node.condition')].at(-1);
  const id = condition.dataset.nodeId;
  const before = condition.offsetLeft;
  condition.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, button:0, clientX:620, clientY:350}));
  window.dispatchEvent(new PointerEvent('pointermove', {bubbles:true, clientX:680, clientY:390}));
  window.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, clientX:680, clientY:390}));
  const moved = document.querySelector(`[data-node-id='${id}']`).offsetLeft;
  const edge = document.querySelector('.graph-edge');
  const lineThin = Number.parseFloat(getComputedStyle(edge).strokeWidth) === 2;
  document.querySelector('.graph-edge-hit').dispatchEvent(new MouseEvent('click', {bubbles:true}));
  const inspector = document.getElementById('nodeInspector').innerText;
  const marker = document.getElementById('graphArrow');
  const arrowVisible = edge.getAttribute('marker-end') === 'url(#graphArrow)' && marker?.getAttribute('markerWidth') === '10' && marker?.getAttribute('markerUnits') === 'userSpaceOnUse';
  const paletteOnly = [...document.querySelectorAll('.node-template strong')].map(item => item.textContent).join('|') === 'Trigger|Condition|Action|Step';
  const waitTemplate = document.querySelector('.node-template.step');
  const waitTransfer = new DataTransfer();
  waitTemplate.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer:waitTransfer}));
  canvas.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, clientX:820, clientY:350, dataTransfer:waitTransfer}));
  canvas.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, clientX:820, clientY:350, dataTransfer:waitTransfer}));
  const stepVisible = document.getElementById('nodeInspector').innerText.includes('Seconds');
  const behavior = document.querySelector('#nodeInspector select');
  behavior.value = 'step.hold_true';
  behavior.dispatchEvent(new Event('change', {bubbles:true}));
  const holdVisible = document.getElementById('nodeInspector').innerText.includes('Hold true') && document.getElementById('nodeInspector').innerText.includes('Unit');
  const edgesBefore = document.querySelectorAll('.graph-edge').length;
  document.querySelector('.graph-node.trigger .port-right.occupied').click();
  return {
    nodes: document.querySelectorAll('.graph-node').length,
    conditionPorts: document.querySelectorAll('.graph-node.condition .node-port').length,
    moved: moved > before,
    edgeSelectable: inspector.includes('Follows the arrow direction'),
    arrowVisible,
    lineThin,
    paletteOnly,
    stepVisible,
    holdVisible,
    edgesBefore,
    edgesAfter: document.querySelectorAll('.graph-edge').length,
    noPrimary: !document.body.innerText.includes('Primary Door')
  };
})()
'@

    Invoke-Cdp "Emulation.setDeviceMetricsOverride" @{width=320; height=760; deviceScaleFactor=1; mobile=$true} | Out-Null
    Evaluate "location.reload()" | Out-Null
    Start-Sleep -Milliseconds 500
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $mobileReady = Evaluate "document.querySelectorAll('.mobile-node').length"
        if ($mobileReady.result.value -ge 2) { break }
        Start-Sleep -Milliseconds 200
    }
    $mobile = Evaluate "({width:innerWidth,scroll:document.documentElement.scrollWidth,nodes:document.querySelectorAll('.mobile-node').length,minButton:Math.min(...[...document.querySelectorAll('button')].map(button=>button.getBoundingClientRect().height).filter(height=>height>0))})"

    Evaluate "setTimeout(()=>location.href='/',0)" | Out-Null
    Start-Sleep -Milliseconds 500
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $dashboardMobileReady = Evaluate "document.querySelectorAll('.dashboard-module').length"
        if ($dashboardMobileReady.result.value -ge 2) { break }
        Start-Sleep -Milliseconds 200
    }
    $dashboardMobile = Evaluate "({width:innerWidth,scroll:document.documentElement.scrollWidth,modules:document.querySelectorAll('.dashboard-module').length,manual:!!document.querySelector('.manual-actions'),minButton:Math.min(...[...document.querySelectorAll('button')].map(button=>button.getBoundingClientRect().height).filter(height=>height>0))})"

    $result = @{dashboard=$dashboard.result.value; manualRun=$manualRun.result.value; desktop=$desktop.result.value; mobile=$mobile.result.value; dashboardMobile=$dashboardMobile.result.value}
    if (-not $result.dashboard.hasPicker -or -not $result.dashboard.hasTrash -or -not $result.dashboard.noPrimary -or $result.dashboard.beforeManual -or -not ($result.dashboard.selectedModules -contains 'manual') -or $result.dashboard.editControls -ne 2 -or $result.dashboard.afterRemove -ne 1 -or $result.dashboard.afterAdd -ne 2 -or $result.dashboard.reordered[1] -ne 'manual' -or $result.dashboard.persisted[1] -ne 'manual' -or -not $result.dashboard.runButton -or -not $result.manualRun.Contains('completed')) { throw "Dashboard browser check failed: $($result | ConvertTo-Json -Compress -Depth 8)" }
    if ($result.desktop.conditionPorts -ne 4 -or -not $result.desktop.moved -or -not $result.desktop.edgeSelectable -or -not $result.desktop.arrowVisible -or -not $result.desktop.lineThin -or -not $result.desktop.paletteOnly -or -not $result.desktop.stepVisible -or -not $result.desktop.holdVisible -or $result.desktop.edgesBefore -ne 1 -or $result.desktop.edgesAfter -ne 0 -or -not $result.desktop.noPrimary) { throw "Desktop editor browser check failed: $($result.desktop | ConvertTo-Json -Compress -Depth 8)" }
    if ($result.mobile.scroll -gt $result.mobile.width -or $result.mobile.nodes -lt 2 -or $result.mobile.minButton -lt 44) { throw "Mobile editor browser check failed" }
    if ($result.dashboardMobile.scroll -gt $result.dashboardMobile.width -or $result.dashboardMobile.modules -lt 2 -or -not $result.dashboardMobile.manual -or $result.dashboardMobile.minButton -lt 44) { throw "Mobile dashboard browser check failed" }
    $result | ConvertTo-Json -Compress -Depth 8
} finally {
    if ($socket) { $socket.Dispose() }
    if ($edge -and -not $edge.HasExited) { Stop-Process -Id $edge.Id -Force }
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    if (Test-Path -LiteralPath $checkRoot) {
        $resolvedRoot = (Resolve-Path -LiteralPath $checkRoot).Path
        if (-not $resolvedRoot.StartsWith([System.IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase)) {
            throw "Browser check escaped the temporary directory"
        }
        Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
    }
}
