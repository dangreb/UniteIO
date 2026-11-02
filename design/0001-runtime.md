# 0001 — Runtime: aplicações, loops e limites

**Status:** implementado na 0.2.0 (`src/uniteio/runtime.py`, `test/test_runtime.py`)
**Data:** 2026-11-02

## Contexto

O UniteIO tem dois objetivos:

1. Tirar o event loop da MainThread e oferecer uma estrutura thread-safe para
   asyncio no CPython free-threaded.
2. Viabilizar várias aplicações asyncio no mesmo processo, cada uma com seu
   loop numa thread própria, todas compartilhando um único executor.

A implementação atual resolve isso com três mecanismos que acumularam
responsabilidades demais:

- **Singleton por classe (`UIOMeta`).** Ele faz dois trabalhos: identidade ("o
  `TradeStream`", com o construtor servindo de busca) e limite de recursos
  ("não dá para criar mil loops"). O limite é só um efeito colateral e é fácil
  de contornar com `type()` num laço. Ao mesmo tempo, ele proíbe usos legítimos,
  como N workers idênticos. Ainda gera um construtor que às vezes devolve uma
  instância antiga e ignora os kwargs sem avisar, e um metaclass que dá
  `start()` e espera a prontidão segurando um lock global.
- **`UIOPool` herdando de `ThreadPoolExecutor`.** Ele acumula três papéis:
  executor, registro de apps e orquestrador do encerramento. Por isso precisa
  sobrescrever o `shutdown` com uma semântica que não é a de um executor, e daí
  vêm os flags `_uio_shutdown_started`, o Event de conclusão e o caso
  reentrante.

A restrição do singleton foi uma escolha consciente para não ter que responder
"quantos loops são aceitáveis?". Esta nota tenta responder essa pergunta de
outra forma.

## Decisões propostas

### D1. `Runtime` por composição

Uma entidade `Runtime` é dona do executor (ela **tem** um
`ThreadPoolExecutor`, não **é** um) e do registro de apps.

```python
with Runtime(max_loops=..., max_workers=...) as rt:     # ou Runtime.default(), criado sob demanda
    trades = rt.start(TradeStream(state=state))         # construir só configura; start sobe e devolve o app
    rt[TradeStream]                                     # busca explícita (ou rt.get("trades"))
# saída do with = rt.close(): para os apps (e seus loops), depois o executor
```

- **`start()` devolve o próprio app que recebeu.** A assinatura é genérica
  (`def start[A: App](self, app: A) -> A`), então o tipo concreto chega até quem
  chamou. Se a subida falhar, a exceção é propagada e nada fica registrado.
  Iniciar um app que já foi iniciado, neste ou em outro runtime, dá erro.
- **`Runtime` é context manager.** O `__enter__` devolve o próprio runtime e o
  `__exit__` chama `close()`, sem engolir a exceção que estiver propagando. Se o
  `close()` também falhar, a falha dele fica encadeada à original.

- O ciclo de vida é explícito, como no `threading.Thread`: constrói, depois dá
  start. O `__init__` volta a ser um construtor comum, com qualquer assinatura.
  Os atributos da subclasse existem antes de o `__call__` rodar.
- **O `Runtime` é dono das threads.** O app não é uma `Thread` e não carrega a
  estrutura dela. Ele expõe só `thread`, `join()` e `is_alive()`. A subida, a
  falha e o `close()` ficam num lugar só.
- **Os atributos de runtime existem desde a construção.** `runtime`, `loop`,
  `name` e os internos têm `None` como padrão na classe, então o `__init__`
  da subclasse continua livre. Os métodos que exigem um app iniciado
  (`submit`, `stop`, `join`) lançam `RuntimeError("... was not started")`.
  Uma subida que falha volta tudo para `None`.
- **Critério do que volta pelo `start()`:** só o que impede o loop de existir
  (criar ou configurar o loop). Se o loop subiu e é usável, uma falha no
  `__call__` vai para o `exception_handler` do app e o app continua vivo.
- O `close()` tem uma ordem óbvia: parar os apps, fechar os loops, desligar o
  executor.
- Um runtime padrão criado sob demanda mantém o uso simples. Os testes criam o
  próprio `Runtime`, então o `conftest` não precisa mais resetar estado global.

### D2. Identidade é busca explícita, não singleton

- Sai o singleton por classe e o "construtor como busca".
- O registro do `Runtime` indexa por nome (único dentro do runtime) e por classe
  quando houver exatamente uma instância daquela classe.
- Não existe restrição de "um por classe". Se surgir uma necessidade real, ela
  vira uma opção explícita da classe, e não uma regra para todos.
- **O nome é resolvido no `start()`**, nesta ordem: o argumento
  `rt.start(app, name=...)`, um `name = "..."` no corpo da classe e, por
  último, o nome da classe. Várias instâncias da mesma classe usam o argumento.
  Sai o `__init_subclass__`, que só fazia sentido com o singleton.

### D3. Limite de loops: um guarda-corpo que ensina, não uma barreira estrutural

**Do que estamos nos protegendo:** do abuso acidental, não de um teto rígido de
recursos. O risco é alguém (um dev júnior, por exemplo) usar o loop como
*unidade de escala*: um loop por conexão, por requisição, por usuário.

**Princípio:** *loops são topologia, não escala.* A quantidade de loops é
decidida no desenho da aplicação e não cresce com a carga. Quem cresce com a
carga são as tasks.

- **Contar loops parados não mede risco.** Um loop esperando I/O fica
  bloqueado no IOCP ou no epoll e não usa CPU. O custo real está em threads
  *ocupadas* ao mesmo tempo, e isso depende da carga.
- **O guarda-corpo é um teto generoso com uma mensagem que ensina.** Quando
  estoura, o erro explica o padrão ("está criando loops em função da carga? use
  tasks"). O valor exato do teto importa pouco: ele precisa pegar o padrão
  errado sem atrapalhar o uso legítimo.
- **O padrão vem de `os.process_cpu_count()`.** Assim a lib não adivinha o
  contexto da máquina. Quem opera ajusta pelos mecanismos do próprio
  interpretador: `PYTHON_CPU_COUNT` ou `-X cpu_count=N`, ou ainda
  `--cpuset-cpus` no container. É o mesmo critério do padrão do
  `ThreadPoolExecutor` desde o 3.13.
  - Atenção: `docker --cpus` é uma cota de cgroup e **não** muda a afinidade, então
    não aparece no `process_cpu_count()`. Nesse caso, use `PYTHON_CPU_COUNT`.
    Isso precisa estar documentado.
- **É ajustável e desligável:** `Runtime(max_loops=N)` ou `Runtime(max_loops=None)`.
- **Loops e executor têm orçamentos separados**, os dois derivados do mesmo
  `process_cpu_count()`. Eles atendem cargas diferentes: os loops cuidam de
  I/O e callbacks curtos e ficam a maior parte do tempo parados, enquanto o
  executor cuida do trabalho síncrono e bloqueante. Somar os dois num orçamento
  único puniria loops parados por causa de workers ocupados.

### D4. Com GIL: o mesmo modelo, um loop por app

**Decisão:** não existe modo de loop compartilhado. Cada app tem sempre o seu
próprio loop numa thread própria, com ou sem GIL. O GIL muda a performance,
não a semântica.

**Hipótese descartada:** o pool entregar um loop único para todas as apps
quando houvesse GIL. A medição abaixo mostrou que isso piora o isolamento e não
traz ganho que justifique um segundo modelo.

**Medição** (2026-11-02, Windows 11, 3.15.0 e 3.15.0t): uma app que precisa
acordar a cada 10 ms, rodando ao lado de outra que segura a CPU 200 ms por vez.
Valores são o atraso além dos 10 ms, em p99 / máximo:

| Cenário                      | 3.15 com GIL | 3.15t sem GIL |
|------------------------------|--------------|---------------|
| App sozinha (referência)     | 14 / 19 ms   | 15 / 16 ms    |
| Loop compartilhado           | 390 / 590 ms | 390 / 590 ms  |
| Um loop por app              | 41 / 42 ms   | 15 / 16 ms    |

- **No loop compartilhado, a app boa congela.** O asyncio é cooperativo, então
  ela fica parada até a vizinha chegar num `await`. Isso independe do GIL.
- **Com loops separados e GIL, o problema vira atraso limitado.** O GIL é
  preemptivo: a thread ocupada solta o GIL a cada `sys.getswitchinterval()`
  (5 ms por padrão).
- **Sem GIL, os loops separados não percebem a vizinha.**

**Custo de vários loops com GIL**, documentado mas aceito:

- Não há paralelismo de bytecode Python.
- Uma thread acordando de I/O pode esperar até um `switchinterval` pelo GIL.
- Threads paradas em I/O soltam o GIL e não custam CPU. Extensões C que soltam o
  GIL (`ssl`, `zlib`, `hashlib`) ainda rodam em paralelo.

**Consequências para o desenho:**

- Sem modo compartilhado, o app pode continuar sendo dono do seu loop.
  Parar um app é parar o loop dele, e o exception handler por loop já é
  automaticamente por app.
- Não é preciso separar "app" de "host" por causa do GIL. A separação acabou
  vindo por outro motivo: com o `Runtime` dono das threads, a subida, a falha
  e o encerramento ficam num lugar só (ver D1).
- Nunca segurar um `threading.Lock` durante um `await` continua sendo boa
  prática, mas deixa de ser uma armadilha específica de um modo.

### D5. Uma porta entre threads, e as outras dão erro

As ferramentas do asyncio assumem uma única thread. Com GIL, usá-las de outra
thread já era errado, só aparecia menos; sem GIL vira corrida de verdade. Uma
task factory "thread-safe" não resolve, porque ela roda na thread de quem
chamou e precisa devolver a `Task` na hora. O `TaskGroup` também tem estado
próprio que disputaria com o loop.

- **`App.submit` é a porta.** Coroutine, função coroutine (com `*args` e
  `**kwargs`) ou lote de coroutines vão para o loop; um callable vai para o
  executor. A task é criada **na thread do loop**, e as opções `name`,
  `context` e `eager` vão direto ao `create_task`. Por isso não precisa de
  task factory nem de registro por coroutine.
- **Do lado de dentro, asyncio puro.** `create_task`, `TaskGroup` e `await`
  funcionam normalmente. O lote (`TaskGroup`) é só uma conveniência do `submit`,
  equivalente a uma coroutine do usuário.
- **As outras portas dão erro em vez de agir de forma imprevisível.** O loop do
  app é um `GuardedLoop` (o `asyncio.EventLoop` da plataforma), que lança
  `RuntimeError` quando `call_soon`, `call_later`, `call_at` ou `create_task`
  vêm de outra thread. Na thread dona, nada muda. O espaço da task factory fica
  livre para quem usa o app.
- **Esperar o loop pela thread do próprio loop dá erro.** O `submit` devolve um
  `LoopFuture`, cujo `.result()` e `.exception()` lançam `RuntimeError` em vez de
  travar para sempre. Esperar da thread de outro app é permitido, e
  `await asyncio.wrap_future(...)` funciona entre loops.
- **Encerramento sem deixar ninguém esperando.** Cancelar o Future cancela a
  task. O `stop()` cancela o trabalho pendente, inclusive o que foi enviado mas
  não chegou a rodar, e um `submit` depois disso dá `RuntimeError`.
- **Tasks sem nome levam o nome do app** (`"<app>:<n>"`), para os logs e
  tracebacks mostrarem a quem pertencem. Nomes explícitos ficam como vieram.

**Eager e threads:** no `submit`, o primeiro passo de uma task eager roda na
thread do loop, dentro do callback que cria a task, sem segurar nenhum lock. Os
riscos que sobram não são de thread: muda a ordem de execução, há reentrância
com locks segurados por quem cria a task, e eager com `TaskGroup` é uma área
com correções recentes no CPython (gh-128550, gh-155418).

## Correções na base atual (independentes das decisões acima)

Todas estão resolvidas no esboço. Elas valem para a base quando o `Runtime`
substituir o `UniteIO`/`UIOPool`.

1. **Exceções do `__call__` somem.** O Future do `run_coroutine_threadsafe` é
   descartado (`uniteio.py:342`), e o `exception_handler` não vê essa exceção
   (verificado no 3.15).
   *No esboço:* o `__call__` vira uma task com um callback que encaminha a
   falha para `loop.call_exception_handler`, e o handler do app está instalado.
2. **Prontidão sob lock global.** O `UIOMeta.__call__` espera o `_wait`
   segurando o `RLock`. Se a thread falhar antes do `_wait.set()`, a espera trava
   para sempre e o app fica registrado no pool.
   *No esboço:* o `start()` reserva a vaga sob o lock, solta e espera um
   `concurrent.futures.Future` de prontidão. A falha da thread chega a quem
   chamou e nada fica registrado.
3. **`submit`.** Não aceita argumentos posicionais, o que quebra 3 testes e os
   exemplos do README. O nome passado com um callable era descartado sem aviso.
   *No esboço:* ver D5. Aceita `*args`, a forma em lote, e dá `TypeError` para
   opções de task com um callable.
4. **`UIOTask._coroarg` pode vazar.** É um dicionário de classe compartilhado
   entre loops, e fica com entradas órfãs se o agendamento falhar depois de
   `coroargs()`.
   *No esboço:* some junto com a task factory, porque as opções vão direto ao
   `create_task` na thread do loop.
5. **Dependência de detalhes privados do CPython** (`loop._default_executor`).
   *No esboço:* o `BorrowedExecutor` é uma subclasse de `ThreadPoolExecutor`
   que repassa o `submit` ao executor compartilhado e faz do `shutdown` um
   no-op. O `loop.close()` não derruba o pool, e só a API pública é usada.

## Fora do escopo desta nota

- **EmitIO / IterEmitter.** Saem do pacote base. O trabalho em andamento fica
  preservado num branch e volta depois como camada separada, ou vai para a
  aplicação.
- **Reorganização do repositório** (layout `src/`, `wa/`, artefatos de build
  soltos na raiz).

## Em aberto

- **Qual o valor padrão do teto de loops?** Vai ser medido no estudo do `listens`:
  N loops servindo TCP/UDP com o executor compartilhado, N ∈ {1, 2, 4, 8, 16},
  com e sem GIL, na workstation e no servidor Ubuntu. Métricas: throughput,
  latência p50/p99 e uso de CPU.
- **Com GIL, o que a lib comunica?** A base atual emite um `warnings.warn`. Com
  D4, rodar com GIL é um modo suportado. O esboço só expõe `rt.gil_enabled`,
  sem aviso. Falta decidir se isso basta ou se vale um log.
- **A medição do D4 vale para carga de rede real?** Repetir no estudo do
  `listens` com TCP/UDP de verdade, não só com uma app ocupando a CPU.
- **Qual a versão mínima do Python?** Hoje é 3.14.5. Avaliar subir para 3.15 e
  testar com o 3.15t desde o começo.
- **Erros de trabalho "dispara e esquece".** Se ninguém ler o Future de um
  `submit`, a exceção some, como no `ThreadPoolExecutor` e no
  `run_coroutine_threadsafe`. Dá para encaminhar ao `exception_handler` quando o
  Future é descartado sem ninguém ter lido o erro, mas isso muda o contrato.
- **Escolha do loop.** Hoje é sempre o `asyncio.EventLoop` da plataforma. Um
  `Runtime(loop_factory=...)` permitiria uvloop ou um selector no Windows, mas a
  guarda do D5 precisaria funcionar sobre a classe escolhida.
- **Custo da guarda.** Toda task e todo callback do loop passam por um override
  em Python que compara `get_ident()`. Medir no estudo do `listens`, junto com
  o teto de loops.
- **Ctrl+C.** Funciona pelo `with`, porque o `__exit__` chama `close()`, mas não
  tem teste.
