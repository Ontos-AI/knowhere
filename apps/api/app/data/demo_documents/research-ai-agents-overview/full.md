# AI Agents Overview.pdf

## Page 1

AI Agents Overview
Yiying Zhang
Some slides adopted from https://llm-course.github.io/

## Page 2

What is AI Agent?
•From outside: an autonomous entity that can operate on its own
•What’s inside: a program involving model calls and often tool calls
•Diﬀerence from an LLM: can perceive env, make decisions, take actions
•Diﬀerence from an AI workﬂow: Autonomous and can “morph”

## Page 3

What is LLM Agents
The Rise and Potential of Large Language Model Based Agents: A Survey
Scenario of an envisioned society composed of AI agents
In the kitchen, one agent
orders dishes, while
another agent is responsible
for planning and solving
the cooking task. At the
concert, three agents are
collaborating to perform in
a band. Outdoors, two
agents are discussing
lantern-making, planning
the required materials,
and finances by selecting
and using tools. Users can
participate in any of these
stages of this social activity

## Page 4

The use cases for LLM agents, or Language Model-based agents, are vast and diverse. These
agents, powered by large language models (LLMs), can be used in various scenarios, including:
1.Single-agent applications
2.Multi-agent systems
3.Human-Agent cooperation
Category
https://gptpluginz.com/llm-agents/

## Page 5

LLM agents can be utilized as personal assistants to assist users in breaking free from daily
tasks and repetitive labor. They can analyze, plan, and solve problems independently, reducing
the work pressure on individuals and enhancing task-solving efficiency.
Single-agent applications
https://github.com/langchain-ai/langchain

## Page 6

Multi-agent systems:LLM agents can interact with each other in a collaborative or competitive manner.
This enables them to achieve advancement through teamwork or adversarial interactions. In these systems,
agents can work together on complex tasksor compete against each otherto improve their performance.
Multi-agent systems

## Page 7

Human-Agent cooperation:LLM agents can interact with humans, providing them with
assistance and performing tasks more efficiently and safely.
Example: interactively write code together with ChatGPT.
Human-Agent cooperation
https://gptpluginz.com/llm-agents/

## Page 8

Agent-to-Agent (A2A) Protocol
●A protocol enabling standardized
communication across agents
●Support agents using different
frameworks to communicate
●A client (local) agent can discover
agents by fetching “Agent Card”
of available remote agents
●And then delegate a task to the
chosen remote agent
●Supports streaming and asynchronous
push notifications for long tasks

## Page 9

Here is a famous picture from Lilian Weng (from OpenAI)
What is LLM Agents
https://gptpluginz.com/llm-agents/
https://lilianweng.github.io/posts/2023-06-23-agent/

## Page 10

Planning:
ƔSubgoal and decomposition:The agent breaks down large tasks into smaller, manageable
subgoals, enabling efficient handling of complex tasks.
ƔReflection and refinement:The agent can do self-criticism and self-reflection over past
actions, learn from mistakes and refine them for future steps, thereby improving the quality
of final results.
What is LLM Agents
https://gptpluginz.com/llm-agents/
https://lilianweng.github.io/posts/2023-06-23-agent/

## Page 11

Task Decomposition: Chain of thought
Chain of Thought (CoT) has become a standard prompting technique for enhancing model
performance on complex tasks. The model is instructed to ³think step by step´to utilize more test-
time computation to decompose hard tasks into smaller and simpler steps. CoT transforms big
tasks into multiple manageable tasks and shed lights into an interpretation of the model¶s thinking
process.
Chain-of-Thought Prompting Elicits Reasoning in Large Language Models

## Page 12

Task Decomposition: Tree of Thoughts
Tree of Thoughts extends CoT by exploring multiple reasoning possibilities at each step. It first
decomposes the problem into multiple thought steps and generates multiple thoughts per step,
creating a tree structure. The search process can be BFS (breadth-first search) or DFS (depth-first
search) with each state evaluated by a classifier (via a prompt) or majority vote.
Tree of Thoughts: Deliberate Problem Solving with Large Language Models

## Page 13

Self-Reflection: ReACT
ReACT integrates reasoning and acting within LLM by extending the action space to be a
combination of task-specific discrete actions and the language space. The former enables LLM
to interact with the environment (e.g. use Wikipedia search API), while the latter prompting LLM
to generate reasoning traces in natural language.
The ReAct prompt template incorporates explicit steps for LLM to think, roughly formatted as:
ReAct: Synergizing Reasoning and Acting in Language Models

## Page 14

Self-Reflection: ReACT
In both experiments on knowledge-intensive tasks and decision-making tasks, ReActworks better
than the Act-only baseline where 7KRXJKW«step is removed.
ReAct: Synergizing Reasoning and Acting in Language Models

## Page 15

Memory:
ƔShort-term memory:all the in-context learning is
utilizing short-term memory of the model to learn.
ƔLong-term memory:this provides the agent with
the capability to retain and recall (infinite)
information over extended periods, often by
leveraging an external vector store and fast retrieval.
What is LLM Agents
https://gptpluginz.com/llm-agents/
https://lilianweng.github.io/posts/2023-06-23-agent/

## Page 16

LLM Agent Memory: Types of Memory in LLMs
1.Sensory Meory:learning embedding representations for raw inputs, including text, image or
other modalities;[Vision encoder/speech encoder]
2.Short-Term Memory (STM):in-context learning. It is short and finite, as it is restricted by
the finite context window length of Transformer.[prompt engineering]
3.Long-Term Memory (LTM):the external vector store that the agent can attend to at query
time, accessible via fast retrieval. [Retrieval-augmentedLMs ]

## Page 17

Agent Memory Types
●Semantic: facts and general
knowledge
●Episodic: past requests/actions
●Procedural: skills defined in
programs or fine-tuned
●Short-term: context within a
request execution
https://www.newsletter.swirlai.com/p/simple-way-to-explain-memory-in-ai

## Page 18

Limitations of LLM without external data (Why RAG)
●Static: LLM’s knowledge is frozen at the time of training
●No proprietary info: LLM usually trained with only public data
●Non-attributable: Difficult to trace the source of an LLM generation
●Hallucination: Easier to hallucinate when asked about knowledge outside
training set

## Page 19

Retrieve-Augmented Generation (RAG)
●Data source
●Can be structured data, raw documents, images, videos, etc.
●Preprocessed by chunking and embedding, usually stored in a vector DB
●Retrieve
●Convert query into embedding
●Get the chunk from the DB with highest similarity with the embedding
●Augment and generate
●Summarize and combine the retrieved chunks with existing context
●Fed to downstream LLMs or tools in an agent

## Page 20

source: https://www.designveloper.com/blog/what-is-retrieval-augmented-generation/

## Page 21

Why retrieval-based LMs?
LLMs are large and expensive to train and run
e.g., RETRO (Borgeaud et al., 2021): ³obtains comparable
performance to GPT-3 on the Pile, despite using 25x fewer
parameters´
Long-term goal:can we possibly reduce the training
and inference costs, and scale down the size of LLMs?

## Page 22

Tool use:
ƔThe agent learns to call external APIsfor extra information that is missing from the model
weights (often hard to change after pre-training), including current information, code
execution capability, access to proprietary information sources and more.
What is LLM Agents
https://gptpluginz.com/llm-agents/
https://lilianweng.github.io/posts/2023-06-23-agent/

## Page 23

Equipping Agents: The Power of Tooling
●Tools: external functions, APIs, or even another (utility) agent (e.g., MCP
server)
●Agents can often decide when to call tools and what tools to call
●Common tools
○Web search + crawling
○Browser
○Social media, email hooks
○Code + CLI execution

## Page 24

https://lilianweng.github.io/posts/2023-06-23-agent/
Action:
ƔThe agent's ability to execute actions in the real or virtual world is crucial. This can range
from performing tasks in a digital environment to controlling physical robots or devices.
The execution phase relies on the agent's planning, memory, and tool use to carry out
tasks effectively and adaptively.
What is LLM Agents https://gptpluginz.com/llm-agents/

## Page 25

ƔLanguage Mastery:Their inherent capability to both
comprehend and produce language ensures seamless
user interaction.
ƔDecision-making:LLMs are equipped to reason and
decide, making them adept at solving intricate issues.
ƔFlexibility:Their adaptability ensures they can be
molded for diverse applications.
ƔCollaborative Interactions:They can collaborate
with humans or other agents, paving the way for
multifaceted interactions.
Why LLM Agents stand out?

## Page 26

HuggingGPT: Solving AI Tasks with ChatGPT and its Friends in Hugging Face
LLMs + APIs to expert models: HuggingGPT

## Page 27

HuggingGPT: Solving AI Tasks with ChatGPT and its Friends in Hugging Face
The system comprises of 4 stages:
ƔTask Planning:LLM analyze the user's
requests, breaking them down into solvable
tasks through prompts.
ƔModel Selection:LLM is presented with a
list of models to choose from and distributes
the tasks to expert models. LLM.
ƔTask Execution:Expert models execute on
the specific tasks and log results.
ƔResponse Generation:LLM receives the
execution results and provides summarized
results to users.
LLMs + APIs to expert models: HuggingGPT

## Page 28

TALM: Tool Augmented Language Models
TALM: Tool Augmented Language Models
LLMs + training for tool use: TALM

## Page 29

Toolformer: Language Models Can Teach Themselves to Use Tools
LLMs + training for tool use: Toolformer

## Page 30

MCP (Model Context Protocol)
●Connecting (N) LLMs to (M) external tools/resources used to be a NxM problem
●MCP standardizes the LLM-tool communication into a N->1->M process
●Build with a client-server model
●MCP client: the agent that needs to call tool/data
●MCP server: a service to expose external tools and data sources

## Page 31

AI Agent/Workflow Frameworks
●Frameworks initially proposed to standardize AI workflows, provide some out-of-box
design patterns and abstractions
●Some examples
●LangChain: Came out the earliest, probably the most popular and hardest to use
●LlamaIndex: Good RAG support
●CrewAI and Camel: multi-agent framework for more complex tasks
●But a lot of unnecessary, added complexity for agents, harder to customize
●My experience of what’s the easiest and sufficient for many tasks
●No framework (pure Python)
●No MCP (can just write your own functions or hooks)
●No A2A (no need for multi-agent)

## Page 32

What is AI Agent Infra?
●Agent testing and evaluation
○Unit + e2e test, metrics, benchmarks, human-in-the-loop
●Agent autotuning and optimization
○Automated prompt tuning, model selection, tool selection, workflow
optimization
●Agent hosting
○Serverless or long-running?
○Stateful or stateless?
●Tooling, memory, data

## Page 33

Demo Time: Eigent Computer-Use
Agent performing a Discord
summarization task
