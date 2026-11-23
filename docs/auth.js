/* LukClips · conta do cliente (Supabase Auth)
   Precisa do supabase-js carregado antes:
   <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script> */
(() => {
  const SUPABASE_URL = "https://fcgtbdmpsqfusdmepvzh.supabase.co";
  /* chave pública "anon" (ou "publishable") do projeto: Supabase → Project Settings → API Keys.
     Ela é feita para ficar no site, não é segredo. */
  const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZjZ3RiZG1wc3FmdXNkbWVwdnpoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTUzNjE4MjksImV4cCI6MjExMDkzNzgyOX0.wQSPCyAuLl0ddneYVSVzobbg_D-YtZPzb9dLnLGJgCc";
  const LOJA = SUPABASE_URL + "/functions/v1/loja";

  const sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    auth: {persistSession: true, autoRefreshToken: true, detectSessionInUrl: true}
  });

  const traduz = msg => {
    const m = String(msg || "").toLowerCase();
    if (m.includes("invalid login")) return "E-mail ou senha incorretos.";
    if (m.includes("already registered") || m.includes("already been registered")) return "Esse e-mail já tem conta. Clique em \"Já tenho conta\" e entre com sua senha.";
    if (m.includes("at least 6") || m.includes("password should")) return "A senha precisa ter pelo menos 6 caracteres.";
    if (m.includes("not confirmed")) return "Confirme seu e-mail pelo link que enviamos (veja o spam) e entre de novo.";
    if (m.includes("valid email") || m.includes("invalid format")) return "Digite um e-mail válido.";
    if (m.includes("rate limit") || m.includes("too many")) return "Muitas tentativas. Espere um minuto e tente de novo.";
    if (m.includes("failed to fetch") || m.includes("network")) return "Sem conexão. Confira sua internet e tente de novo.";
    return msg || "Algo deu errado. Tente de novo.";
  };

  const sessao = async () => (await sb.auth.getSession()).data.session;

  /* chamada à função "loja" já com o login do cliente */
  const api = async corpo => {
    const s = await sessao();
    const headers = {"Content-Type": "application/json"};
    if (s) headers.Authorization = "Bearer " + s.access_token;
    const r = await fetch(LOJA, {method: "POST", headers, body: JSON.stringify(corpo)});
    return r.json();
  };

  /* monta o formulário de criar conta / entrar dentro de um elemento.
     opcoes.aoEntrar(sessao) roda quando o login dá certo. */
  const montarForm = (el, opcoes = {}) => {
    let modo = opcoes.modo || "criar";
    el.innerHTML = `
      <form class="cform" novalidate>
        <p class="ctit"></p>
        <p class="csub"></p>
        <label>E-mail<input type="email" name="email" required autocomplete="email" placeholder="voce@email.com"></label>
        <label>Senha<input type="password" name="senha" required minlength="6" placeholder="mínimo 6 caracteres"></label>
        <button class="btn btn-main" type="submit"></button>
        <p class="cerr" hidden></p>
        <p class="cok" hidden></p>
        <div class="clinks">
          <button type="button" class="clink" data-troca></button>
          <button type="button" class="clink" data-esqueci hidden>Esqueci minha senha</button>
        </div>
      </form>`;
    const f = el.querySelector("form"), q = s => f.querySelector(s);
    const msg = (cls, t) => { q(cls).textContent = t || ""; q(cls).hidden = !t; };
    const pintar = () => {
      const criar = modo === "criar";
      q(".ctit").textContent = criar ? (opcoes.tituloCriar || "Crie sua conta rápida") : "Entre na sua conta";
      q(".csub").textContent = criar
        ? (opcoes.subCriar || "Leva 10 segundos. Seu serial e suas compras ficam salvos nela.")
        : "Use o e-mail e a senha que você cadastrou.";
      q("[type=submit]").textContent = criar ? (opcoes.botaoCriar || "Criar conta") : (opcoes.botaoEntrar || "Entrar");
      q("[data-troca]").textContent = criar ? "Já tenho conta → Entrar" : "Não tenho conta → Criar agora";
      q("[data-esqueci]").hidden = criar;
      q("[name=senha]").autocomplete = criar ? "new-password" : "current-password";
      msg(".cerr"); msg(".cok");
    };
    q("[data-troca]").addEventListener("click", () => { modo = modo === "criar" ? "entrar" : "criar"; pintar(); });
    q("[data-esqueci]").addEventListener("click", async () => {
      const email = q("[name=email]").value.trim();
      msg(".cerr"); msg(".cok");
      if (!email) return msg(".cerr", "Digite seu e-mail acima e clique de novo em \"Esqueci minha senha\".");
      const {error} = await sb.auth.resetPasswordForEmail(email, {redirectTo: new URL("conta.html", location.href).href});
      if (error) msg(".cerr", traduz(error.message));
      else msg(".cok", "Enviamos um link para " + email + " para você criar uma senha nova (veja o spam).");
    });
    f.addEventListener("submit", async e => {
      e.preventDefault();
      const b = q("[type=submit]"); if (b.disabled) return;
      const email = q("[name=email]").value.trim(), senha = q("[name=senha]").value;
      msg(".cerr"); msg(".cok");
      if (!/^\S+@\S+\.\S+$/.test(email)) return msg(".cerr", "Digite um e-mail válido.");
      if (senha.length < 6) return msg(".cerr", "A senha precisa ter pelo menos 6 caracteres.");
      const texto = b.textContent; b.disabled = true; b.textContent = "Aguarde...";
      try {
        const r = modo === "criar"
          ? await sb.auth.signUp({email, password: senha, options: {emailRedirectTo: new URL("conta.html", location.href).href}})
          : await sb.auth.signInWithPassword({email, password: senha});
        if (r.error) throw r.error;
        if (!r.data.session) {
          /* o Supabase está pedindo confirmação de e-mail */
          modo = "entrar"; pintar(); q("[name=email]").value = email;
          msg(".cok", "Conta criada! Abra o link que enviamos para " + email + " (veja o spam) e depois entre aqui.");
          return;
        }
        opcoes.aoEntrar && opcoes.aoEntrar(r.data.session);
      } catch (err) { msg(".cerr", traduz(err.message)); }
      finally { b.disabled = false; b.textContent = texto; }
    });
    pintar();
    return {focar: () => q("[name=email]").focus()};
  };

  window.LukConta = {sb, sessao, api, montarForm, traduz, sair: () => sb.auth.signOut()};
})();
