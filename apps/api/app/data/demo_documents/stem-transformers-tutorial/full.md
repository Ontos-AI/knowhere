## INTRODUCTION TO TRANSFORMERS

Jesús Cerquides (IIIA-CSIC)

(2/07/2024)

## Who am I?

http://www.iiia.csic.es/\~cerquide

![](images/16f96c0c295e0e3b407a8bd261af93e1f3c4d7fb66ac9c7b8ea6a2dd5d57b180.jpg)

<details>
<summary>natural_image</summary>

Circular portrait of a smiling man with short hair and beard, set against green foliage background (no text or symbols)
</details>

Jesus Cerquides

Scientific Researcher, IIIA/CSIC

cerquide@iia.csic.es

+34 934840520

![](images/bb9cb3d6a262f58612dcd75b928e7254ab5dc776279ac3beacdf9380c95f6d64.jpg)

## Interests

• AI for Participatory Sciences & Democracy  
• Probabilistic Graphical Models  
- Causal Inference  
• Machine Learning in Health  
• Quantum Machine Learning

## Academia

BarcelonaTech 1996 - 2003

Ph.D. Artificial Intelligence

UAB 2008 - 2011

Degree Mathematics

UAB prize to the best student in that promotion

BarcelonaTech 1990 - 1995

M. Sc. Computer Science

Spanish prize to the best student in CS in the country that year

## What I hope you will learn

• Transformers and composites  
- The main tasks you can solve with transformers  
- How these tasks can be solved and main transformer constructs and layers.  
- Opening transformer layers. How do transformers work internally.

## Attention Timeline

![](images/9b70b684a8dc0531343195713db57726e193ac766decc4908de1107e72bcaf76.jpg)

<details>
<summary>bar chart</summary>

| Era | Description |
|---|---|
| 1990s | Prehistoric Era |
| 2014 | Simple attention mechanisms |
| 2017 | Beginning of transformers |
| 2018 | Explosion of transformers in NLP (BERT, GPT-3) |
| 2018-2020 | Explosion into other fields (ViTs, Alphafold-2.) |
| 2021-2022 | Start of Generative Era (Codex, Decision Transformers, GPT-X, DALL-E) |
| 2024 | Present Day (Future ?!) |
Huge models, more applications: Chat-GPT, GPT-4, Gemini, Llama and open-source LLMs, Whisper, Robotics Transformer, Stable Diffusion, Sora, and so much more...!
</details>

## What are transformers?

Transformers are a neural network architecture for working with composites:

Composite: Something that is made up of distinct parts or elements.

Examples of composites:

Text:

The elements of a sentence are words

Images:

![](images/792509474ebe079ba4d5cac68841c35bc8404ee2d4ecfe10fa0c2e1fb36cf37c.jpg)

<details>
<summary>natural_image</summary>

Abstract geometric composition of a blue fish shape composed of triangular segments (no text or symbols)
</details>

Patches

Sound:

![](images/1796be54449385d096f1783f359043e2077ce3389c5d663d3c36b1670d6b5e7d.jpg)

<details>
<summary>line chart</summary>

| Time Segment | Frequency (s) |
| ------------ | ------------- |
| 0            | 0             |
| 1            | 0.5           |
| 2            | 0.3           |
| 3            | 0.7           |
| 4            | 0.2           |
| 5            | 0.8           |
| 6            | 0.4           |
| 7            | 0.6           |
| 8            | 0.9           |
| 9            | 0.1           |
| 10           | 0.5           |
| 11           | 0.3           |
| 12           | 0.7           |
| 13           | 0.2           |
| 14           | 0.8           |
| 15           | 0.4           |
| 16           | 0.6           |
| 17           | 0.9           |
| 18           | 0.1           |
| 19           | 0.5           |
| 20           | 0.3           |
| 21           | 0.7           |
| 22           | 0.2           |
| 23           | 0.8           |
| 24           | 0.4           |
| 25           | 0.6           |
| 26           | 0.9           |
| 27           | 0.1           |
| 28           | 0.5           |
| 29           | 0.3           |
| 30           | 0.7           |
| 31           | 0.2           |
| 32           | 0.8           |
| 33           | 0.4           |
| 34           | 0.6           |
| 35           | 0.9           |
| 36           | 0.1           |
| 37           | 0.5           |
| 38           | 0.3           |
| 39           | 0.7           |
| 40           | 0.2           |
| 41           | 0.8           |
| 42           | 0.4           |
| 43           | 0.6           |
| 44           | 0.9           |
| 45           | 0.1           |
| 46           | 0.5           |
| 47           | 0.3           |
| 48           | 0.7           |
| 49           | 0.2           |
| 50           | 0.8           |
| 51           | 0.4           |
| 52           | 0.6           |
| 53           | 0.9           |
| 54           | 0.1           |
| 55           | 0.5           |
| 56           | 0.3           |
| 57           | 0.7           |
| 58           | 0.2           |
| 59           | 0.8           |
| 60           | 0.4           |
| 61           | 0.6           |
| 62           | 0.9           |
| 63           | 0.1           |
| 64           | 0.5           |
| 65           | 0.3           |
| 66           | 0.7           |
| 67           | 0.2           |
| 68           | 0.8           |
| 69           | 0.4           |
| 70           | 0.6           |
| 71           | 0.9           |
| 72           | 0.1           |
| 73           | 0.5           |
| 74           | 0.3           |
| 75           | 0.7           |
| 76           | 0.2           |
| 77           | 0.8           |
| 78           | 0.4           |
| 79           | 0.6           |
| 80           | 0.9           |
| 81           | 0.1           |
| 82           | 0.5           |
| 83           | 0.3           |
| 84           | 0.7           |
| 85           | 0.2           |
| 86           | 0.8           |
| 87           | 0.4           |
| 88           | 0.6           |
| 89           | 0.9           |
| 90           | 0.1           |
| 91           | 0.5           |
| 92           | 0.3           |
| 93           | 0.7           |
| 94           | 0.2           |
| 95           | 0.8           |
| 96           | 0.4           |
| 97           | 0.6           |
| 98           | 0.9           |
| 99           | 0.1           |
| Note: The actual values are not provided in the code image, so they are estimated from the visual data source table to match the original data table numbers in the code format.
</details>

Frames

Each of these elements are referred to generically as tokens.

The process of breaking a composite into tokens is known as tokenization.

## What can we do with transformers?

Natural Language Processing: text classification, named entity recognition, question answering, language modeling, summarization, translation, multiple choice, and text generation.

Computer Vision: image classification, object detection, and segmentation.

Audio: automatic speech recognition and audio classification.

Multimodal: table question answering, optical character recognition, information extraction from scanned documents, video classification, and visual question answering.

## What can we do with transformers?

Classify composites

Complete composites

Transform composites

# What can we do with transformers?

# Classify composites

Task: Given a composite classify it as belonging to one specific class

Examples:

\- Image classification

Inputs  
![](images/ae7f8369c76ad08c91d92c569d5d8fcd8fe8b72bbe5401b5ca9d76ad4e61c3d0.jpg)

<details>
<summary>natural_image</summary>

A tabby cat standing on grassy ground under a blue sky (no text or symbols visible)
</details>

Image Classification Model

## Output

Egyptian cat

0.514

Tabby cat

0.193

Tiger cat

0.068

# What can we do with transformers?

# Classify composites

Task: Given a composite classify it as belonging to one specific class

Examples:

\- Text classification

![](images/550a658b777bbed2de6c9008ff881ad37937337453c9cc021de791a52e554cd5.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["Inputs"] --> B["Input\nI love Hugging Face!"]
  B --> C["Text\nClassification\nModel"]
  C --> D["Output\nPOSITIVE 0.900\nNEUTRAL 0.100\nNEGATIVE 0.000"]
```
</details>

# What can we do with transformers?

# Complete composites

Task: Given a composite with one (or several) missing token/s, complete it in a reasonable way.

Examples:

\- Text generation (Causal Language Modeling).

Autoregressive  
![](images/49f70ae81e38f5447986295fd7db80a99cb554824d31895e77cc6affa32b653d.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["Inputs"] --> B["Input"]
  B --> C["Once upon a time, Text Generation Model"]
  C --> D["Output"]
  D --> E["My"]
  D --> F["My"]
  D --> G["My"]
  D --> H["My"]
  D --> I["my"]
  D --> J["name"]
  D --> K["name"]
  D --> L["name"]
  D --> M["is"]
  D --> N["is"]
  D --> O["Sylvain"]
  D --> P["Sylvain"]
  D --> Q["."]
    style A fill:#f9f,stroke:#333
    style B fill:#ccf,stroke:#333
    style C fill:#cfc,stroke:#333
    style D fill:#fcc,stroke:#333
    style E fill:#ffc,stroke:#333
    style F fill:#cfc,stroke:#333
    style G fill:#cfc,stroke:#333
    style H fill:#cfc,stroke:#333
    style I fill:#cfc,stroke:#333
    style J fill:#cfc,stroke:#333
    style K fill:#cfc,stroke:#333
    style L fill:#cfc,stroke:#333
    style M fill:#cfc,stroke:#333
    style N fill:#cfc,stroke:#333
    style O fill:#cfc,stroke:#333
    style P fill:#cfc,stroke:#333
    style Q fill:#cfc,stroke:#333
```
</details>

# What can we do with transformers?

# Complete composites

Task: Given a composite with one (or several) missing token/s, complete it in a reasonable way.

Examples:

\- Masked language modeling (Fill-Mask):

## Inputs

Input

The <mask> barked at me

Fill-

Mask

Model

## Output

wolf

![](images/c2765d873fff9b0a90e3431a4d619721bc980fb2d2c40473571655340095ff81.jpg)

dog

![](images/8678e2a01b848e3365bd5c9edfb3038e85a66c4f0efce4a704f4cb9ac6bd466a.jpg)

cat

![](images/38d70e1e9c53b624c2e4d2d041d3df4867566bde40253dc7ef867001a9f46915.jpg)

fox

![](images/6f24714ce444bcff45a721a400eda46736e7c4fe0c854c019869f54b267a035a.jpg)

squirrel

0.487

0.061

0.058

0.047

0.025

## What can we do with transformers?

## Complete composites

Task: Given a composite with one (or several) missing token/s, complete it in a reasonable way.

Examples:

\- Image completion (Image inpainting):

![](images/34a850c89eb7690442a5486883cd9292af88224ff6ea6ef481b285140025383f.jpg)

## What can we do with transformers?

## Transform composites

Task: Given a composite transform it into a different one.

Examples:

\- Translation

## Inputs

## Input

My name is Omar and I

live in Zürich.

## Translation Model

## Output

## Output

Mein Name ist Omar

und ich wohne in

Zürich.

# What can we do with transformers?

# Transform composites

Task: Given a composite transform it into a different one.

Examples:

\- Summarization

## Inputs

## Input

The tower is 324 metres (1,063 ft) tall, about the same height as an 81-storey building, and the tallest structure in Paris. Its base is square, measuring 125 metres (410 ft) on each side. It was the first structure to reach a height of 300 metres. Excluding transmitters, the Eiffel Tower is the second tallest free-standing structure in France after the Millau Viaduct.

## Summarization Model

## Output

## Output

The tower is 324 metres (1,063 ft) tall, about the same height as an 81-storey building. It was the first structure to reach a height of 300 metres.

## What can we do with transformers?

## Transform composites

Task: Given a composite transform it into a different one.

Examples:

\- Image to Text

Inputs  
![](images/f15da2b36aaca35c92a1390432ad3d0469b2a37001b84bab28ee8cd445fa4c6f.jpg)

<details>
<summary>natural_image</summary>

A herd of giraffes and zebras grazing in a savanna landscape with no visible text or symbols.
</details>

Image-to-Text Model

## Output

Detailed description

a herd of giraffes and

zebras grazing in a field

## What can we do with transformers?

## Transform composites

Task: Given a composite transform it into a different one.

Examples:

\- Text to Image

## Inputs

## Input

A city above clouds, pastel colors, Victorian style

Text-to-Image Model

## Output

![](images/879ee726be93ecb5e97555139af165533152f73d013d716897b91d6e3250ebd4.jpg)

<details>
<summary>natural_image</summary>

Fantasy cityscape with ornate architecture and glowing clouds, no visible text or symbols
</details>

https://www.kdnuggets.com/2020/10/understanding-transformers-data-science-way.html

https://www.aprendemachinelearning.com/como-funcionan-los-transformers-espanol-nlp-gpt-bert/

## What can we do with transformers?

Classify composites

Complete composites

Transform composites

## How to classify composites with transformers?

1. Obtain a “good” semantic representation of the composite.  
2. Use it to classify.

Ummm, representations... what do we mean by that???

## Representations matter

![](images/b04eb1f97ec0a20f5a0cc997fda540c1ba634a3437acfcaddd2b9516302b85a3.jpg)

<details>
<summary>scatter plot</summary>

| x1       | x2       | Group |
| -------- | -------- | ----- |
| (various) | (various) | Blue  |
| (various) | (various) | Green |
</details>

![](images/9035a2595a4544fe6d9ac729efc53b03224ad735663c5c9024e2f5624efc6ccb.jpg)

<details>
<summary>scatter plot</summary>

| Group | x1² | x2² |
|-------|-----|-----|
| Green | 0.1 | 0.3 |
| Green | 0.2 | 0.4 |
| Green | 0.3 | 0.5 |
| Green | 0.4 | 0.6 |
| Green | 0.5 | 0.7 |
| Green | 0.6 | 0.8 |
| Green | 0.7 | 0.9 |
| Green | 0.8 | 1.0 |
| Blue    | 0.1 | 0.2 |
| Blue    | 0.2 | 0.3 |
| Blue    | 0.3 | 0.4 |
| Blue    | 0.4 | 0.5 |
| Blue    | 0.5 | 0.6 |
| Blue    | 0.6 | 0.7 |
| Blue    | 0.7 | 0.8 |
| Blue    | 0.8 | 0.9 |
| Blue    | 0.9 | 1.0 |
| Blue    | 1.0 | 1.1 |
| Blue    | 1.1 | 1.2 |
| Blue    | 1.2 | 1.3 |
| Blue    | 1.3 | 1.4 |
| Blue    | 1.4 | 1.5 |
| Blue    | 1.5 | 1.6 |
| Blue    | 1.6 | 1.7 |
| Blue    | 1.7 | 1.8 |
| Blue    | 1.8 | 1.9 |
| Blue    | 1.9 | 2.0 |
| Blue    | 2.0 | 2.1 |
| Blue    | 2.1 | 2.2 |
| Blue    | 2.2 | 2.3 |
| Blue    | 2.3 | 2.4 |
| Blue    | 2.4 | 2.5 |
| Blue    | 2.5 | 2.6 |
| Blue    | 2.6 | 2.7 |
| Blue    | 2.7 | 2.8 |
| Blue    | 2.8 | 2.9 |
| Blue    | 2.9 | 3.0 |
| Blue    | 3.0 | 3.1 |
| Blue    | 3.1 | 3.2 |
| Blue    | 3.2 | 3.3 |
| Blue    | 3.3 | 3.4 |
| Blue    | 3.4 | 3.5 |
| Blue    | 3.5 | 3.6 |
| Blue    | 3.6 | 3.7 |
| Blue    | 3.7 | 3.8 |
| Blue    | 3.8 | 3.9 |
| Blue    | 3.9 | 4.0 |
| Blue    | 4.0 | 4.1 |
| Blue    | 4.1 | 4.2 |
| Blue    | 4.2 | 4.3 |
| Blue    | 4.3 | 4.4 |
| Blue    | 4.4 | 4.5 |
| Blue    | 4.5 | 4.6 |
| Blue    | 4.6 | 4.7 |
| Blue    | 4.7 | 4.8 |
| Blue    | 4.8 | 4.9 |
| Blue    | 4.9 | 5.0 |
| Blue    | 5.0 | 5.1 |
| Blue    | 5.1 | 5.2 |
| Blue    | 5.2 | 5.3 |
| Blue    | 5.3 | 5.4 |
| Blue    | 5.4 | 5.5 |
| Blue    | 5.5 | 5.6 |
| Blue    | 5.6 | 5.7 |
| Blue    | 5.7 | 5.8 |
| Blue    | 5.8 | 5.9 |
| Blue    | 5.9 | 6.0 |
| Blue    | 6.0 | 6.1 |
| Blue    | 6.1 | 6.2 |
| Blue    | 6.2 | 6.3 |
| Blue    | 6.3 | 6.4 |
| Blue    | 6.4 | 6.5 |
| Blue    | 6.5 | 6.6 |
| Blue    | 6.6 | 6.7 |
| Blue    | 6.7 | 6.8 |
| Blue    | 6.8 | 6.9 |
| Blue    | 6.9 | 7.0 |
| Blue    | 7.0 | 7.1 |
| Blue    | 7.1 | 7.2 |
| Blue    | 7.2 | 7.3 |
| Blue    | 7.3 | 7.4 |
| Blue    | 7.4 | 7.5 |
| Blue    | 7.5 | 7.6 |
| Blue    | 7.6 | 7.7 |
| Blue    | 7.7 | 7.8 |
| Blue    | 7.8 | 7.9 |
| Blue    | 7.9 | 8.0 |
| Blue    | 8.0 | 8.1 |
| Blue    | 8.1 | 8.2 |
| Blue    | 8.2 | 8.3 |
| Blue    | 8.3 | 8.4 |
| Blue    | 8.4 | 8.5 |
| Blue    | 8.5 | 8.6 |
| Blue    | 8.6 | 8.7 |
| Blue    | 8.7 | 8.8 |
| Blue    | 8.8 | 8.9 |
| Blue    | 8.9 | 9.0 |
| Blue    | 9.0 | 9.1 |
| Blue    | 9.1 | 9.2 |
| Blue    | 9.2 | 9.3 |
| Blue    | 9.3 | 9.4 |
| Blue    | 9.4 | 9.5 |
| Blue    | 9.5 | 9.6 |
| Blue    | 9.6 | 9.7 |
| Blue    | 9.7 | 9.8 |
| Blue    | 9.8 | 9.9 |
| Blue    | 9.9 |10.0 |
The chart displays a scatter plot with two distinct clusters of data points labeled by 'Feature Space'. The x-axis is labeled 'x₁²' and the y-axis is labeled 'x₂²'. The data points are grouped into three groups: 'Blue', 'Green', and 'Blue' respectively, with each group's cluster positioned at its respective x and y positions on the plot.
</details>

Figure 2: Representations matter for shallow machine learning models such as Logistic Regression. A simple transformation, e.g. squaring the values of the raw features, may be enough to solve the problem.

## Representation learning

Default Representation  
![](images/5e5232c9f97302d6f5e299eb19b85b9bd50434e6f4f6a6c74c8f879167f43085.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Cat: 1"] --> D["Group 1"]
  B["Dog: 2"] --> D
  C["Cat: 3"] --> D
  E["Dog: 4"] --> D
  F["Cat: 5"] --> D
  G["Dog: 6"] --> D
  H["Cat: 7"] --> D
  I["Dog: 8"] --> D
  J["Cat: 9"] --> D
  K["Dog: 10"] --> D
  L["Cat: 11"] --> D
  M["Dog: 12"] --> D
  N["Cat: 13"] --> D
  O["Dog: 14"] --> D
  P["Cat: 15"] --> D
  Q["Dog: 16"] --> D
  R["Cat: 17"] --> D
  S["Dog: 18"] --> D
  T["Cat: 19"] --> D
  U["Dog: 20"] --> D
  V["Cat: 21"] --> D
  W["Dog: 22"] --> D
  X["Cat: 23"] --> D
  Y["Dog: 24"] --> D
  Z["Cat: 25"] --> D
  AA["Dog: 26"] --> D
  AB["Cat: 27"] --> D
  AC["Dog: 28"] --> D
  AD["Cat: 29"] --> D
  AE["Dog: 30"] --> D
  AF["Cat: 31"] --> D
  AG["Dog: 32"] --> D
  AH["Cat: 33"] --> D
  AI["Dog: 34"] --> D
  AJ["Cat: 35"] --> D
  AK["Dog: 36"] --> D
  AL["Cat: 37"] --> D
  AM["Dog: 38"] --> D
  AN["Cat: 39"] --> D
  AO["Dog: 40"] --> D
  AP["Cat: 41"] --> D
  AQ["Dog: 42"] --> D
  AR["Cat: 43"] --> D
  AS["Dog: 44"] --> D
  AT["Cat: 45"] --> D
  AU["Dog: 46"] --> D
  AV["Cat: 47"] --> D
  AW["Dog: 48"] --> D
  AX["Cat: 49"] --> D
  AY["Dog: 50"] --> D
```
</details>

Deep Neural Network

"Good" Semantic Representation  
![](images/bfc4685f8707debd956c198b82b81be7e81e1e78d3f4cd6c4b80b14b23cb4c66.jpg)

<details>
<summary>text_image</summary>

Diagram showing cat and dog illustrations with directional arrows, likely illustrating a concept or relationship between animals.
</details>

Cat by Martin LEBRETON, Dog by Serhii Smirnov from the Noun Project

How can we build a “good” semantic representation for composites?

We can use an encoder based on transformer layers.

## How can we build a “good” semantic representation for composites? Encoders

1. Obtain a representation of the composite. It does not have to be “good” or semantic. An initial representation.  
2. Feed the representation through several transformer layers

## How can be build a representation for composites?

Text:

The elements of a sentence are words

Images:

![](images/4fefd71ce3c2c05fd34930054ffc610400615bd010d1ef85420723d9a213a68a.jpg)

<details>
<summary>natural_image</summary>

Abstract geometric composition of a blue fish shape composed of triangular segments (no text or symbols)
</details>

Patches

Sound:

![](images/511060c4138d3581f366e7b815d7009f84129e7ad446c8f829bc7b8f15fc8602.jpg)

<details>
<summary>line chart</summary>

| Time Segment | Frequency (s) |
| ------------ | ------------- |
| 0            | 0             |
| 1            | 0.5           |
| 2            | 0.3           |
| 3            | 0.7           |
| 4            | 0.2           |
| 5            | 0.8           |
| 6            | 0.4           |
| 7            | 0.6           |
| 8            | 0.9           |
| 9            | 0.1           |
| 10           | 0.5           |
| 11           | 0.3           |
| 12           | 0.7           |
| 13           | 0.2           |
| 14           | 0.8           |
| 15           | 0.4           |
| 16           | 0.6           |
| 17           | 0.9           |
| 18           | 0.1           |
| 19           | 0.5           |
| 20           | 0.3           |
| 21           | 0.7           |
| 22           | 0.2           |
| 23           | 0.8           |
| 24           | 0.4           |
| 25           | 0.6           |
| 26           | 0.9           |
| 27           | 0.1           |
| 28           | 0.5           |
| 29           | 0.3           |
| 30           | 0.7           |
| 31           | 0.2           |
| 32           | 0.8           |
| 33           | 0.4           |
| 34           | 0.6           |
| 35           | 0.9           |
| 36           | 0.1           |
| 37           | 0.5           |
| 38           | 0.3           |
| 39           | 0.7           |
| 40           | 0.2           |
| 41           | 0.8           |
| 42           | 0.4           |
| 43           | 0.6           |
| 44           | 0.9           |
| 45           | 0.1           |
| 46           | 0.5           |
| 47           | 0.3           |
| 48           | 0.7           |
| 49           | 0.2           |
| 50           | 0.8           |
| 51           | 0.4           |
| 52           | 0.6           |
| 53           | 0.9           |
| 54           | 0.1           |
| 55           | 0.5           |
| 56           | 0.3           |
| 57           | 0.7           |
| 58           | 0.2           |
| 59           | 0.8           |
| 60           | 0.4           |
| 61           | 0.6           |
| 62           | 0.9           |
| 63           | 0.1           |
| 64           | 0.5           |
| 65           | 0.3           |
| 66           | 0.7           |
| 67           | 0.2           |
| 68           | 0.8           |
| 69           | 0.4           |
| 70           | 0.6           |
| 71           | 0.9           |
| 72           | 0.1           |
| 73           | 0.5           |
| 74           | 0.3           |
| 75           | 0.7           |
| 76           | 0.2           |
| 77           | 0.8           |
| 78           | 0.4           |
| 79           | 0.6           |
| 80           | 0.9           |
| Note: The actual values may vary due to the random nature of the data generation.
</details>

Frames

Each of these
elements are referred
to generically as
tokens

We can represent each token as a vector of D real numbers.

The process of going from tokens to vectors of real numbers is called embedding

## Embeddings

![](images/39277f202f504eb40480d058bc5671afc2fc74c09625fae896c35a40e74d9152.jpg)

<details>
<summary>text_image</summary>

high alignment
low alignment
</details>

Embedding is a means of representing tokens as points in a continuous vector space where the locations of those points in space are semantically meaningful to machine learning (ML) algorithms.

## Word embeddings

![](images/a5a07cb515642e78a95046e9ba767dc43ec6d2c5f32947a13c9cbd9748728d84.jpg)

<details>
<summary>text_image</summary>

king
man
woman
queen
</details>

Male-Female

![](images/5c8fbf6aeff378397bfd66f7533aa17ef04d43dd0652c94ac625c02fd88b625c.jpg)

<details>
<summary>text_image</summary>

walking
walked
swam
swimming
</details>

Verb Tense

![](images/2b737961ad888f05be36bc9b064fc9d3adcae7acbe87de8a23220155547ee340.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Ankara"] --> B["Turkey"]
  B --> C["Canada"]
  C --> D["Italy"]
  D --> E["Rome"]
  E --> F["Germany"]
  F --> G["Berlin"]
  G --> H["China"]
  H --> I["Beijing"]
  I --> J["Tokyo"]
  J --> K["Japan"]
  K --> L["Vietnam"]
  L --> M["Moscow"]
  M --> N["Russia"]
  N --> O["Tourkey"]
  O --> P["Ottawa"]
  P --> Q["Madrid"]
  Q --> R["Spain"]
  R --> S["Italy"]
```
</details>

Country-Capital

## How can we build a representation for composites?

1. Tokenize the composite (divide it into tokens).  
2. Embed each of the token as a vector.

"This is a input text."

Tokenization

<table><tr><td>[CLS]</td><td>This</td><td>is</td><td>a</td><td>input</td><td>.</td><td>[SEP]</td></tr><tr><td>101</td><td>2023</td><td>2003</td><td>1037</td><td>7953</td><td>1012</td><td>102</td></tr></table>

![](images/613aa0a44d18f3ccd87f576fcbe3af2cbc07ceda42f7f4a68f811845bfe3bed3.jpg)

Embeddings

<table><tr><td>0.0390,-0.0123,-0.0208,...</td><td>-0.0558,0.0151,0.0031,...</td><td>-0.0440,-0.0236,-0.0283,...</td><td>0.0119,-0.0037,-0.0402,...</td><td>0069,0.0057,-0.0016,...</td><td>0.0199,-0.0095,-0.0099,...</td><td>-0.0788,0.0202,-0.0352,...</td></tr></table>

![](images/4bfdbe8b9a81f237ba90cfecdb9957992353d4618d3a104e1ebc41d361b94845.jpg)

## How can we build a “good” semantic representation for composites? Encoders

1. Obtain a representation of the composite. It does not have to be “good” or semantic. An initial representation.  
2. Feed the representation through several transformer layers to obtain “context-aware” embeddings.

![](images/923c033c91c209fe3e039e0c5fa39d27c417ed524029979a575244bcb067da5c.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["hello"] --> B["Transformer Encoder"]
  C["world"] --> B
  D["Context-aware embeddings"] --> B
  B --> E["Layer 1"]
  B --> F["Layer 2"]
  B --> G["Layer 3"]
  B --> H["Layer 4"]
  B --> I["Layer 5"]
  B --> J["Layer 6"]
    style A fill:#d4edda,stroke:#333
    style C fill:#d4edda,stroke:#333
    style D fill:#d4edda,stroke:#333
    style B fill:#e6f7ff,stroke:#333
    style E fill:#e6f7ff,stroke:#333
    style F fill:#e6f7ff,stroke:#333
    style G fill:#e6f7ff,stroke:#333
    style H fill:#e6f7ff,stroke:#333
    style I fill:#e6f7ff,stroke:#333
```
</details>

## What are “context-aware” embeddings

The embedding of a token captures its “meaning”.

In a composite, the meaning of a token depends on other tokens

The humanoid robot did not cross the road, as it was very dangerous.

The humanoid robot did not cross the road because it was tired.

## How do transformer layers deal with context?

Transformer layers allows for the exchange of information between tokens. We can understand it as:

1. Each token sends a message to every other token.  
2. Each token computes how much does it want to take into account the messages of each other token. This is the idea of attention.  
3. Each token moves away from his embedding in the direction signaled by the message of each other token weighted by the attention that he is paying to that token.

## How do transformer layers deal with context? Attention

![](images/7b6205a20a3038203bce53b4e0cff3d011df9251b14d4520c0e0c8740be4c5eb.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["it"] --> B["the"]
  A --> C["animal"]
  A --> D["didn't"]
  A --> E["cross"]
  A --> F["the"]
  A --> G["street"]
  A --> H["because"]
  A --> I["it"]
  A --> J["was"]
  A --> K["too"]
  A --> L["tired"]
  A --> M["."]
```
</details>

![](images/3dac2a9d07a7fe94da91fd5c1b6056034af4f54ceb429bdea84a2cb5700d16d8.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["it"] --> B["the"]
  A --> C["animal"]
  A --> D["didn't"]
  A --> E["cross"]
  A --> F["the"]
  A --> G["street"]
  A --> H["because"]
  A --> I["it"]
  A --> J["was"]
  A --> K["too"]
  A --> L["wide"]
  A --> M["."]
```
</details>

## Text classification with transformers

![](images/a8eb0cbe1ca0a2dec86fc90227a46b558ee2bfecd9b8ac85a1832a9cc3500c5e.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["input sequence"] --> B["word embedding"]
  B --> C["position embedding"]
  C --> D["transformer block"]
  D --> E["transformer block"]
  E --> F["transformer block"]
  F --> G["transformer block"]
  G --> H["transformer block"]
  H --> I["transformer block"]
  I --> J["transformer block"]
  J --> K["transformer block"]
  K --> L["transformer block"]
  L --> M["transformer block"]
  M --> N["transformer block"]
  N --> O["transformer block"]
  O --> P["transformer block"]
  P --> Q["transformer block"]
  Q --> R["transformer block"]
  R --> S["transformer block"]
  S --> T["transformer block"]
  T --> U["transformer block"]
  U --> V["transformer block"]
  V --> W["transformer block"]
  W --> X["transformer block"]
  X --> Y["transformer block"]
  Y --> Z["transformer block"]
  Z --> AA["transformer block"]
  AA --> AB["transformer block"]
  AB --> AC["transformer block"]
  AC --> AD["transformer block"]
  AD --> AE["transformer block"]
  AE --> AF["transformer block"]
  AF --> AG["transformer block"]
  AG --> AH["transformer block"]
  AH --> AI["transformer block"]
  AI --> AJ["transformer block"]
  AJ --> AK["transformer block"]
  AK --> AL["transformer block"]
  AL --> AM["transformer block"]
  AM --> AN["transformer block"]
  AN --> AO["transformer block"]
  AO --> AP["transformer block"]
  AP --> AQ["transformer block"]
  AQ --> AR["transformer block"]
  AR --> AS["transformer block"]
  AS --> AT["transformer block"]
  AT --> AU["transformer block"]
  AU --> AV["transformer block"]
  AV --> AW["transformer block"]
  AW --> AX["transformer block"]
  AX --> AY["transformer block"]
  AY --> AZ["transformer block"]
  AZ --> BA["transformer block"]
  BA --> BB["transformer block"]
  BB --> BC["transformer block"]
  BC --> BD["transformer block"]
  BD --> BE["transformer block"]
  BE --> BF["transformer block"]
  BF --> BG["transformer block"]
  BG --> BH["transformer block"]
  BH --> BI["transformer block"]
  BI --> BJ["transformer block"]
  BJ --> BK["transformer block"]
  BK --> BL["transformer block"]
  BL --> BM["transformer block"]
  BM --> BN["transformer block"]
  BN --> BO["transformer block"]
  BO --> BP["transformer block"]
  BP --> BQ["transformer block"]
  BQ --> BR["transformer block"]
  BR --> BS["transformer block"]
  BS --> BT["transformer block"]
  BT --> BU["transformer block"]
  BU --> BV["transformer block"]
  BV --> BW["transformer block"]
  BW --> BX["transformer block"]
  BX --> BY["transformer block"]
  BY --> BZ["transformer block"]
  BZ --> CA["transformer block"]
  CA --> CB["transformer block"]
  CB --> CC["transformer block"]
  CC --> CD["transformer block"]
  CD --> CE["transformer block"]
  CE --> CF["transformer block"]
  CF --> CG["transformer block"]
  CG --> CH["transformer block"]
  CH --> CI["transformer block"]
  CI --> CJ["transformer block"]
  CJ --> CK["transformer block"]
  CK --> CR["transformer block"]
  CR --> CS["transformer block"]
  CS --> CT["transformer block"]
  CT --> CU["transformer block"]
  CU --> CV["transformer block"]
  CV --> CW["transformer block"]
  CW --> CX["transformer block"]
  CX --> CY["transformer block"]
  CY --> CZ["transformer block"]
  CZ --> DA["transformer block"]
  DA --> DB["transformer block"]
  DB --> DC["transformer block"]
  DC --> DD["transformer block"]
  DD --> DE["transformer block"]
  DE --> DF["transformer block"]
  DF --> DG["transformer block"]
  DG --> DH["transformer block"]
  DH --> DI["transformer block"]
  DI --> DJ["transformer block"]
  DJ --> DK["transformer block"]
  DK --> DL["transformer block"]
  DL --> DJ
  DJ --> DK
```
</details>

## Text classification with transformers using BERT

Devlin, Jacob, Ming-Wei Chang, Kenton Lee, and Kristina Toutanova. "BERT: Pre-Training of Deep Bidirectional Transformers for Language Understanding." arXiv, May 24, 2019. https://doi.org/10.48550/arXiv.1810.04805.

![](images/651e0c365fa628aec03f5abc65471d49b684b4b62c32cf1740abca063ff94ab0.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
    subgraph Transformer layer
  A["LSM"] --> B["transformer layer"]
  C["LSM"] --> B
  D["LSM"] --> B
  E["..."] --> B
  F["..."] --> B
  G["..."] --> B
  H["..."] --> B
    end

  I["embedding"] --> J["positive encoding"]
  K["x1"] --> L["embedding"]
  M["xN"] --> N["embedding"]
  O["postional encoding"] --> P["× class"]
  Q["y1"] --> R["× class"]
  S["yN"] --> T["× class"]
  U["c"] --> V["LSM"]
  W["y1"] --> X["LSM"]
  Y["..."] --> Z["..."]
  AA["yN"] --> AB["LSM"]
  AC["L50/arXiv.1"] --> AD["wei and MBERT: formers for ding.&quot;"]
  AE["L5 layers"] --> AF["50/arXiv.1"]
```
</details>

![](images/32f14cd746f286370629a1ed2182fa38550865c623a3f0087e9b3ea5d17d31d4.jpg)

<details>
<summary>text_image</summary>

Christopher M. Bishop
with Hugh Bishop
Deep Learning
Foundations
and Concepts
Springer
</details>

Bishop, Christopher M., and Hugh Bishop. Deep Learning: Foundations and Concepts. Cham: Springer International Publishing, 2024. https://doi.org/10.1007/978-3-031-45468-4.

## How do transformer layers deal with context?

Transformer layers allows for the exchange of information between tokens. We can understand it as:

1. Each token sends a message to every other token.  
2. Each token computes how much does it want to take into account the messages of each other token. This is the idea of attention.  
3. Each token moves away from his embedding in the direction signaled by the message of each other token weighted by the attention that he is paying to that token.

This is a general intuition. To understand better we need to understand how the encoder is trained.

## BERT training MLM

Output

MLM teaches BERT to understand relationships between words

![](images/91e6b32f458e2b0efc03843e43d4a23379c548b416f0962a141515dcab0f1759.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["CLS"] --> B["how"]
  B --> C["are"]
  C --> D["box"]
  D --> E["doing"]
  E --> F["today"]
  F --> G["SEP"]
  H["..."] --> I["you,they,your.."]
  I --> D
  J["CLS"] --> K["how"]
  K --> L["are"]
  L --> M["MASK"]
  M --> N["doing"]
  N --> O["today"]
  O --> P["SEP"]
  Q["..."] --> R["..."]
  R --> D
    style D fill:#ffcccc,stroke:#333
```
</details>

One of the key techniques used in pre-training BERT is the Masked Language Model (MLM). In this approach, a certain percentage of the input tokens are randomly selected (usually around 15% of the tokens) and masked (replaced with a [MASK] token). The objective of the MLM is to predict the original masked words based on the surrounding context. Hence, BERT is useful for completing composites (MLM).

## BERT training NSP

![](images/f2a867f468e5fac9548d479f01674534645165b06009df6581c256e911efb003.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Class Label"] --> B["C"]
  A --> C["T₁"]
  B --> D["Tₙ"]
  C --> E["T[SEP"]]
  D --> F["M₁"]
  E --> G["Mₘ"]
  H["BERT"] --> I["Ecls"]
  H --> J["E₁"]
  H --> K["TK 1"]
  H --> L["TK N"]
  H --> M["TK 1"]
  H --> N["TK N"]
    I <--> O["[CLS"]]
    J <--> P["Tok 1"]
    K <--> Q["TK N"]
    L <--> R["TK 1"]
    M <--> S["TK 1"]
    N <--> T["TK N"]
  U["First Sentence"] --> V["[CLS"]]
  W["Second Sentence"] --> X["Tok 1"]
  Y["Class Label"] --> Z["C"]
  Y --> AA["T₁"]
  Z --> AB["Tₙ"]
  Z --> AC["T[SEP"]]
  Z --> AD["M₁"]
  Z --> AE["Mₘ"]
```
</details>

NSP teaches BERT to understand longer-term dependencies across sentences.

## Text classification with transformers using BERT

Now we can understand this better!

![](images/7bd7272a28cb9af46a5e942c2216a0c19e498be403698df05d1e86279a415849.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A[" stand "] --> B[" Transformer layer "]
  B --> C[" L layers "]
  C --> D[" positioning encoding "]
  D --> E[" embedding "]
  E --> F[" <class> "]
  F --> G[" x1 "]
  G --> H[" embedding "]
  H --> I[" xN "]
  I --> J[" embedded "]
  J --> K[" + "]
  K --> L[" positional encoding "]
  L --> M[" ... "]
  M --> N[" ... "]
  N --> O[" ... "]
  O --> P[" ... "]
  P --> Q[" ... "]
  Q --> R[" ... "]
  R --> S[" ... "]
  S --> T[" ... "]
  T --> U[" ... "]
  U --> V[" ... "]
  V --> W[" ... "]
  W --> X[" ... "]
  X --> Y[" ... "]
  Y --> Z[" ... "]
  Z --> AA[" ... "]
  AA --> AB[" ... "]
  AB --> AC[" ... "]
  AC --> AD[" ... "]
  AD --> AE[" ... "]
  AE --> AF[" ... "]
  AF --> AG[" ... "]
  AG --> AH[" ... "]
  AH --> AI[" ... "]
  AI --> AJ[" ... "]
  AJ --> AK[" ... "]
  AK --> AL[" ... "]
  AL --> AM[" ... "]
  AM --> AN[" ... "]
  AN --> AO[" ... "]
  AO --> AP[" ... "]
  AP --> AQ[" ... "]
  AQ --> AR[" ... "]
  AR --> AS[" ... "]
  AS --> AT[" ... "]
  AT --> AU[" ... "]
  AU --> AV[" ... "]
  AV --> AW[" ... "]
  AW --> AX[" ... "]
  AX --> AY[" ... "]
  AY --> AZ[" ... "]
  AZ --> BA[" ... "]
  BA --> BB[" ... "]
  BB --> BC[" ... "]
  BC --> BD[" ... "]
  BD --> BE[" ... "]
  BE --> BF[" ... "]
  BF --> BG[" ... "]
  BG --> BH[" ... "]
  BH --> BI[" ... "]
  BI --> BJ[" ... "]
  BJ --> BK[" ... "]
  BK --> BL[" ... "]
  BL --> BM[" ... "]
  BM --> BN[" ... "]
  BN --> BO[" ... "]
  BO --> BP[" ... "]
  BP --> BQ[" ... "]
  BQ --> BR[" ... "]
  BR --> BS[" ... "]
  BS --> BT[" ... "]
  BT --> BU[" ... "]
  BU --> BV[" ... "]
  BV --> BW[" ... "]
  BW --> BX[" ... "]
  BX --> BY[" ... "]
  BY --> BZ[" ... "]
  BZ --> CA[" ... "]
  CA --> CB[" ... "]
  CB --> CC[" ... "]
  CC --> CD[" ... "]
  CD --> CE[" ... "]
  CE --> CF[" ... "]
  CF --> CG[" ... "]
  CG --> CH[" ... "]
  CH --> CI[" ... "]
  CI --> CJ[" ... "]
  CJ --> CK[" ... "]
  CK --> CL[" ... "]
  CL --> CM[" ... "]
  CM --> CN[" ... "]
  CN --> CO[" ... "]
  CO --> CP[" ... "]
  CP --> CQ[" ... "]
  CQ --> CR[" ... "]
  CR --> CS[" ... "]
  CS --> CT[" ... "]
  CT --> CU[" ... "]
  CU --> CV[" ... "]
  CV --> CW[" ... "]
  CW --> CX[" ... "]
  CX --> CY[" ... "]
  CY --> CZ[" ... "]
  CZ --> DA[" ... "]
  DA --> DB[" ... "]
  DB --> DC[" ... "]
  DC --> DD[" ... "]
  DD --> DE[" ... "]
  DE --> DF[" ... "]
  DF --> DG[" ... "]
  DG --> DH[" ... "]
  DH --> DI[" ... "]
  DI --> DJ[" ... "]
  DJ --> DK[" ... "]
  DK --> DL[" ... "]
  DL --> DJ
```
</details>

![](images/2abd90f4364b1fc654973a2651345af26f60e4da603ce4c3efed35522bbdf2b6.jpg)

<details>
<summary>text_image</summary>

Christopher M. Bishop
with Hugh Bishop
Deep Learning
Foundations
and Concepts
Springer
</details>

Bishop, Christopher M., and Hugh Bishop. Deep Learning: Foundations and Concepts. Cham: Springer International Publishing, 2024. https://doi.org/10.1007/978-3-031-45468-4.

Interested on fine tuning BERT for classification? Read more

Sun, Chi, Xipeng Qiu, Yige Xu, and Xuanjing Huang. “How to Fine-Tune BERT for Text Classification?” arXiv, February 5, 2020. https://doi.org/10.48550/arXiv.1905.05583.

## Image classification with ViT

![](images/9af6e9c261a164a839fbaa31512e982466d113265e4fa63bc77f778744dde33b.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Input Class 0"] --> B["Transformer Encoder"]
  C["Input Class 1"] --> B
  D["Input Class 2"] --> B
  E["Input Class 3"] --> B
  F["Input Class 4"] --> B
  G["Input Class 5"] --> B
  H["Input Class 6"] --> B
  I["Input Class 7"] --> B
  J["Input Class 8"] --> B
  K["Input Class 9"] --> B
  L["Final Embeddings"] --> B
  M["Extra learnable [class"] embedding] --> A
  N["Patch + Position Embedding"] --> O["Linear Projection of Flattened Patches"]
  P["Image of Dog in Top Left"] --> Q["Output Image"]
```
</details>

ViT process with the learnable class embedding highlight (left).

Dosovitskiy, Alexey, Lucas Beyer, Alexander Kolesnikov, Dirk Weissenborn, Xiaohua Zhai, Thomas Unterthiner, Mostafa Dehghani, et al. “An Image Is Worth 16x16 Words: Transformers for Image Recognition at Scale.” arXiv, June 3, 2021. https://doi.org/10.48550/arXiv.2010.11929.

## What can we do with transformers?

Classify composites

![](images/a2022e03aaba96a775fa40dee681dee38555d51f69e5b912053bd93c7198b992.jpg)

<details>
<summary>natural_image</summary>

Green circular icon with a white checkmark (no text or symbols)
</details>

Complete composites

![](images/e4898148206e2f0f2e13db0d6b39667d940e641737a75c70b0235ea59b365aa7.jpg)

<details>
<summary>natural_image</summary>

Wooden box with orange 3D sculpture on a wooden table, no visible text or symbols
</details>

Transform composites

## How to generate composites? Decoders

![](images/4c1b5adb83d799dbbade5e031cf9fe92921e01eaa2886125504938d80ddf35eb.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["This is an example of how concise I can be"] --> B["Encoder"]
  B --> C["Preprocessing steps"]
  C --> D["Input text"]
  D --> E["This is an _ of how concise I _ be"]
  E --> F["Fills in the missing words to generate the original sentence"]
  F --> G["Receives inputs where words are randomly masked during training"]
```
</details>

![](images/ca4d95b16a805a175dfa87eb760e1a1aaaa916e40cafdad8d92304cde2db8e13.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Decoder"] --> B["Preprocessing steps"]
  B --> C["Input text"]
  C --> D["This is an example of how concise I can be"]
  D --> E["This is an example of how concise I can"]
  E --> F["Receives incomplete texts"]
    style A fill:#cce5ff,stroke:#333
    style B fill:#cce5ff,stroke:#333
    style C fill:#cce5ff,stroke:#333
    style D fill:#cce5ff,stroke:#333
    style E fill:#cce5ff,stroke:#333
    note right of A: "This is an example of how concise I can be"
    note right of D: "Learns to generate one word at a time"
```
</details>

![](images/235b28288b819ad457aa59deace15575b2dfdff06e309009840d23b24e5ef24c.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
SEBASTIAN RUSCHKA
FRANK
SCOTIA
MORNING
</details>

Raschka, Sebastian. Build a Large Language Model (From Scratch). Manning, 2024.

## How to generate composites? Decoders

![](images/5c8c746d9a6b8a9f78242e4a4100510f36f4df70e304b28a83841b16650269e8.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
    subgraph L Layers
  y1["LM"] --> mask_TransformerLayer["Masked transformer layer"]
  y2["LM"] --> maskTransformerLayer["Masked transformer layer"]
  yN+1["LM"] --> maskTransformerLayer["Masked transformer layer"]
    end

    subgraph PositionalEncoding
        direction TB
  E1["embedding"] --> E2["+"]
  E2 --> E3["×start"]
  E3 --> x1["x1"]
  x1 --> Embedding1["embedding"]
  Embedding1 --> Embedding2["+"]
  Embedding2 --> EmbeddingN["embedding"]
  EmbeddingN --> xN["xN"]
    end

    style L Layers fill:#f9f9f9,stroke:#333
    style PositionalEncoding fill:#e6f7ff,stroke:#333
```
</details>

![](images/244885387dd9db9ba525e340a06aa38a0045ed3cdaa28ed4271bd1bfa78242ad.jpg)

<details>
<summary>text_image</summary>

Christopher M. Bishop
with Hugh Bishop

Deep Learning

Foundations
and Concepts

Springer
</details>

Bishop, Christopher M., and Hugh Bishop. Deep Learning: Foundations and Concepts. Cham: Springer International Publishing, 2024. https://doi.org/10.1007/978-3-031-45468-4.

## What is masked attention?

![](images/ff644bcdd520b65012076796ecfe97ea0ccc35fab2e49123a617db492132e156.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["The animal"] --> B["the animal didn't cross the street because it was too tired"]
  A --> C["the street because it was too tired"]
  D["The animal didn't cross the street because it was too wide"] --> E["The animal didn't cross the street because it was too wide"]
  F["The animal didn't cross the street because it was too wide"] --> G["The animal didn't cross the street because it was too wide"]
```
</details>

## What is masked attention?

How much token “Life” attends to token “first”  
![](images/47ddf65ba17bb232887b0f4514b5aa395dcb1f856b8424698bebd91860735b90.jpg)

<details>
<summary>heatmap</summary>

| | Life | is | short | eat | desert | first |
|---|---|---|---|---|---|---|
| Life | 0.17 | 0.13 | 0.18 | 0.16 | 0.15 | 0.18 |
| is | 0.03 | 0.68 | 0.02 | 0.08 | 0.14 | 0.02 |
| short | 0.19 | 0.06 | 0.25 | 0.14 | 0.11 | 0.23 |
| eat | 0.15 | 0.21 | 0.14 | 0.16 | 0.17 | 0.14 |
| desert | 0.13 | 0.27 | 0.11 | 0.16 | 0.18 | 0.12 |
| first | 0.19 | 0.02 | 0.31 | 0.11 | 0.07 | 0.27 |
</details>

In the row for corresponding to "Life", mask out all words that come after "Life"  
![](images/b054eafcf6e8ce9a25a0328d852b83e50e1011c29943a6a90e2b2ade8617f5a6.jpg)

<details>
<summary>heatmap</summary>

| | Life | is | short | eat | desert | first |
|---|---|---|---|---|---|---|
| Life | 0.17 | 0.13 | 0.18 | 0.16 | 0.15 | 0.18 |
| is | 0.03 | 0.68 | 0.02 | 0.08 | 0.14 | 0.02 |
| short | 0.19 | 0.06 | 0.25 | 0.14 | 0.11 | 0.23 |
| eat | 0.15 | 0.21 | 0.14 | 0.16 | 0.17 | 0.14 |
| desert | 0.13 | 0.27 | 0.11 | 0.16 | 0.18 | 0.12 |
| first | 0.19 | 0.02 | 0.31 | 0.11 | 0.07 | 0.27 |
</details>

## How to generate composites? Decoders

![](images/5a0db77149dd02dd9681944f4fefbc340d851e0c1fca81822bddb1c6478a4c11.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Create the next word based on the input text"] --> B["This is"]
  B --> C["Decoder"]
  C --> D["Preprocessing steps"]
  D --> E["Input text"]
  E --> F["This"]
    
  G["This is an"] --> H["Output layers"]
  H --> I["Decoder"]
  I --> J["Preprocessing steps"]
  J --> K["Input text"]
    
  L["This is an example"] --> M["Output layers"]
  M --> N["Decoder"]
  N --> O["Preprocessing steps"]
  O --> P["Input text"]
    
  Q["This is an"] --> R["This is"]
  R --> S["The output of the previous round serves as input to the next round"]
    
    style A fill:#f9f,stroke:#333
    style B fill:#ccf,stroke:#333
    style C fill:#cfc,stroke:#333
    style D fill:#fcc,stroke:#333
    style E fill:#cff,stroke:#333
    style F fill:#ffc,stroke:#333
    style G fill:#f9f,stroke:#333
    style H fill:#ccf,stroke:#333
    style I fill:#cfc,stroke:#333
    style J fill:#fcc,stroke:#333
    style K fill:#ffc,stroke:#333
    style L fill:#f9f,stroke:#333
    style M fill:#ccf,stroke:#333
    style N fill:#cfc,stroke:#333
    style O fill:#fcc,stroke:#333
    style P fill:#ffc,stroke:#333
    style Q fill:#f9f,stroke:#333
```
</details>

![](images/ef7e447b85a4b2c0fd7d824d7a3f215089fcca52b9c255e4234dff3e73021791.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
Sebastian Raschka
STONY SCOTKA
MENNING
</details>

Raschka, Sebastian. Build a Large Language Model (From Scratch). Manning, 2024.

Autoregressive

## How are decoders trained?

## The model is simply trained to predict the next word

![](images/50f1211e1fb141b9acbfd2a8b75d5d53ae0dabe715987fe16d2c85763d2dfaaf.jpg)

![](images/6feb88dd89c4bc5f7f78cd92169294ad16710a53b8126a3b418c1f30db022d04.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
Sebastian Ruschka
MORNING
FRANKY SCOTCHA
</details>

Raschka, Sebastian. Build a Large Language Model (From Scratch). Manning, 2024.

## How are decoders efficiently trained?

![](images/886d23fb1d6c2dcaf23241f846d90b01ae8a5af13689876aa03af91dabfba87a.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Text sample:"] --> B["Input the LLM receives"]
  B --> C1["LLMs learn to predict one word at a time"]
  B --> C2["LLMs learn to predict one word at a time"]
  B --> C3["LLMs learn to predict one word at a time"]
  B --> C4["LLMs learn to predict one word at a time"]
  B --> C5["LLMs learn to predict one word at a time"]
  C1 --> D1["The LLM can't access words past the target"]
  C2 --> D2["Target to predict"]
  C3 --> D3["LLMs can't access words past the target"]
  C4 --> D4["LLMs can't access words past the target"]
  C5 --> D5["LLMs can't access words past the target"]
```
</details>

![](images/052606a65a436c63318f9a5dec7022d624f07a3cdb111afa102d796f1c6d70ec.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
Sebastian Raschka
STONY SCOTKA
MENNING
</details>

Raschka, Sebastian.
Build a Large Language
Model (From Scratch).
Manning, 2024.

## What can we do with transformers?

Classify composites

![](images/c168a18b04922c739906661ffb85a8e09e808fb43b6bc65bfe4ba1184d129a7a.jpg)

<details>
<summary>natural_image</summary>

Green circular icon with a white checkmark (no text or symbols)
</details>

Complete composites

![](images/d721444cdeb676963ce44e67f4b90e4a0f86b853154474734188bc179adde91c.jpg)

<details>
<summary>natural_image</summary>

Green circular icon with a white checkmark symbol (no text or numbers)
</details>

Transform composites

## Transforming composites. Encoders and decoders together 8) The complete output

![](images/8772e875e708c2475b63ec0c266b8086a89ce73d3cb4c9c1f38e46584c4fbff8.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["1) The Input text to be translated"] --> B["Preprocessing steps"]
  B --> C["Encoder"]
  C --> D["Embeddings"]
  D --> E["Output layers"]
  E --> F["Decoder"]
  F --> G["Preprocessing steps"]
  G --> H["Input text"]
  H --> I["This is an example"]
    
    style A fill:#ccc,stroke:#333
    style B fill:#ccc,stroke:#333
    style C fill:#ccc,stroke:#333
    style D fill:#ccc,stroke:#333
    style E fill:#ccc,stroke:#333
    style F fill:#ccc,stroke:#333
    style G fill:#ccc,stroke:#333
    style H fill:#ccc,stroke:#333
    style I fill:#ccc,stroke:#333
    
  note1["4) The encoder returns embedding vectors as input to the decoder"] --> C
  note2["5) A partial output text (the model completes the translation one word at a time)"] --> F
  note3["6) The input text is prepared for the decoder"] --> G
  note4["7) The decoder generates the translated text one word at a time"] --> F
  note5["8) The complete output (translation)"] --> F
```
</details>

![](images/777fa7eccaa2bb27125174f7348a073b7cc520c6f33052a1457c1b0d6258e72a.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
Sebastian Ruschka
MORNING
FRANKY SCOTCHA
</details>

Raschka, Sebastian.
Build a Large Language
Model (From Scratch).
Manning, 2024.

## Transforming composites. Encoders and decoders together 8) The complete output

![](images/6eaca321b4bf57d494d77466d7e482948ce43d9a1f4831084cfc4f3961db2df8.jpg)

<details>
<summary>natural_image</summary>

3D white figure sitting on a red question mark, no text or symbols present
</details>

![](images/452b932e19c1affc48c38c5df24088940f7b686d5d0f0a6554574b076ab64558.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["1) The Input text to be translated"] --> B["Preprocessing steps"]
  B --> C["Encoder"]
  C --> D["Embeddings"]
  D --> E["Output layers"]
  E --> F["Decoder"]
  F --> G["Preprocessing steps"]
  G --> H["Input text"]
  H --> I["This is an example"]
  I --> J["Encoder has access to the complete input text to produce text encodings used by the decoder"]
  J --> K["The encoder returns embedding vectors as input to the decoder"]
  K --> L["Embeddings"]
  L --> M["Das ist ein"]
  M --> N["The complete output (translation)"]
  N --> O["Output layers"]
  O --> P["7) The decoder generates the translated text one word at a time"]
  P --> Q["Preprocessing steps"]
  Q --> R["Input text"]
  R --> S["Das ist ein"]
  S --> T["5) A partial output text (the model completes the translation one word at a time)"]
  T --> U["Preprocessing steps"]
  U --> V["Encoder"]
  V --> W["Embeddings"]
  W --> X["Das ist ein"]
```
</details>

![](images/9d0b87610f34117882b1d090fc25f1c960f3a7d764fc495a3f6b82a627aa598f.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
Sebastian Ruschka
MORNING
FRANKY SCOTCHA
</details>

Raschka, Sebastian.
Build a Large Language
Model (From Scratch).
Manning, 2024.

## Decoders vs. conditional decoders

![](images/bce15d7ed213fa869a1dda1c6c6cc82d0a9062ba340aa4e5fb71ced11c5c535b.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["This is an example of how concise I can be"] --> B["Decoder"]
  B --> C["Preprocessing steps"]
  C --> D["Input text"]
  D --> E["This is an example of how concise I can"]
  E --> F["Receives incomplete texts"]
  G["Leads to generate one word at a time"] --> A
```
</details>

Approximate

$$
p (x _ {n} | x _ {1}, \ldots , x _ {n - 1})
$$

![](images/6545f30f046be2159a1a73434ffebdc6e68ff989864f02dda77f2f07f76ff6a8.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Input text"] --> B["Preprocessing steps"]
  B --> C["Decoder"]
  C --> D["Das ist ein"]
  D --> E["Output layers"]
  E --> F["Das ist ein Beispiel"]
  F --> G["8) The complete output (translation)"]
  C --> H["7) The decoder generates the translated text one word at a time"]
  C --> I["6) The input text is prepared for the decoder"]
  I --> J["5) A partial output text (the model completes the translation one word at a time)"]
```
</details>

Approximate

$$
p (x _ {n} | x _ {1}, \dots , x _ {n - 1}, c)
$$

![](images/145bd608d20bbd42e0c75d93052f523387690735054f33493895070e6c0d9d2e.jpg)

<details>
<summary>text_image</summary>

BUILD A
Large Language
Model
Sebastian Ruschka
STOW
EASTRA
</details>

Raschka, Sebastian.  
Build a Large Language  
Model (From Scratch).  
Manning, 2024.

What is the most likely next word if the sentence meaning is c?

## Transforming composites.

![](images/42f2e91f070247f5d80c843e965955afd9c2409ff5347acc148e40ec03d5a0cc.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
    subgraph Encoder[encoder]
  A1["self-attention transformer layer"] --> B1["..."]
  A2["self-attention transformer layer"] --> B2["+"]
  C["embedding"] --> D["X"]
    end
    subgraph Conditional[conditional decoder]
  E1["cross-attention transformer layer"] --> F1["..."]
  E2["cross-attention transformer layer"] --> F2["+"]
  G["embedding"] --> H["{<start>, Y1:N-1}"]
    end
  I["Z"] --> J["self-attention transformer layer"]
  J --> A1
  J --> A2
  K["YN"] --> L["LSM"]
  L --> M["Cross-attention transformer layer"]
  M --> E1
  M --> E2
    style Encoder fill:#f9f,stroke:#333
    style Conditional fill:#bbf,stroke:#333
```
</details>

![](images/b94fc68f074c12a0f0d963d170c858ac8815ced1b147d1e8226cbda2299d8176.jpg)

<details>
<summary>text_image</summary>

Christopher M. Bishop
with Hugh Bishop

Deep Learning

Foundations
and Concepts

Springer
</details>

Bishop, Christopher M., and Hugh Bishop. Deep Learning: Foundations and Concepts. Cham: Springer International Publishing, 2024. https://doi.org/10.1007/978-3-031-45468-4.

## What can we do with transformers?

Classify composites

![](images/b058c8e65abdf9c06b64dac637d43d5e6c80a93c27930d00eeafc120cdbc06d3.jpg)

<details>
<summary>natural_image</summary>

Green circular icon with a white checkmark (no text or symbols)
</details>

Complete composites

![](images/ff8256dab4236e769a5b28431b6ecdd69f089b43f5153382e033f5539b7560bc.jpg)

<details>
<summary>natural_image</summary>

Green circular icon with a white checkmark symbol (no text or numbers)
</details>

Transform composites

![](images/53cd3d5142a0f7f33631d99b5162c4b72f911532f2bfc0e2ec97496f46b99e22.jpg)

<details>
<summary>natural_image</summary>

Green circular icon with a white checkmark symbol (no text or numbers)
</details>

## Opening the box. Understanding transformer layers.

Transformer layer -> Encoders

Masked transformer layer -> Decoders

Cross-attention transformer layer -> Conditional decoders

## Transformer layer

A composite as a matrix

N (tokens)

![](images/5a25bdff0f7a989030e294f50a9c066d3e3ef243272bfd8517be3081d0fa0764.jpg)

<details>
<summary>text_image</summary>

xₙᵀ
X
</details>

D (features)

$\widetilde{\mathbf{X}} = \text{TransformerLayer}[\mathbf{X}]$ .

## Transformer layer

## Algorithm 12.3: Transformer layer

Input: Set of tokens $X \in R^{N \times D} : \{x_{1}, \ldots, x_{N}\}$ Multi-head self-attention layer parameters
Feed-forward network parameters

Output: $\widetilde{\mathbf{X}}\in \mathbb{R}^{N\times D}:\{\widetilde{\mathbf{x}}_1,\dots ,\widetilde{\mathbf{x}}_N\}$

$\mathbf{Z} = \text{LayerNorm}[\mathbf{Y}(\mathbf{X}) + \mathbf{X}] // \mathbf{Y}(\mathbf{X})$ from Algorithm 12.2 $\widetilde{\mathbf{X}} = \text{LayerNorm}[\text{MLP}[\mathbf{Z}] + \mathbf{Z}] // \text{shared neural network}$ return $\widetilde{\mathbf{X}}$

Y(X): Result of applying multi-head self-attention

![](images/76754388f504e5ceb4b0ccc511f666863c72c0f8eff7804c2120be72c5c5a1db.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  X["Input X̃"] --> Add1["add & norm"]
  Add1 --> MLP["MLP"]
  MLP --> Add2["add & norm"]
  Add2 --> MultiHead["multi-head self-attention"]
  MultiHead --> Add3["add & norm"]
  Add3 --> Add4["MLP"]
  Add4 --> Add5["add & norm"]
  Add5 --> Add6["Output X̃"]
  Add6 --> Add7["Output X"]
  Add7 --> Add8["Output X̃"]
  Add8 --> Add9["Output X"]
  Add9 --> Add10["Output X̃"]
  Add10 --> Add11["Output X̃"]
  Add11 --> Add12["Output X̃"]
  Add12 --> Add13["Output X̃"]
  Add13 --> Add14["Output X̃"]
  Add14 --> Add15["Output X̃"]
  Add15 --> Add16["Output X̃"]
  Add16 --> Add17["Output X̃"]
  Add17 --> Add18["Output X̃"]
  Add18 --> Add19["Output X̃"]
  Add19 --> Add20["Output X̃"]
  Add20 --> Add21["Output X̃"]
  Add21 --> Add22["Output X̃"]
  Add22 --> Add23["Output X̃"]
  Add23 --> Add24["Output X̃"]
  Add24 --> Add25["Output X̃"]
  Add25 --> Add26["Output X̃"]
  Add26 --> Add27["Output X̃"]
  Add27 --> Add28["Output X̃"]
  Add28 --> Add29["Output X̃"]
  Add29 --> Add30["Output X̃"]
  Add30 --> Add31["Output X̃"]
  Add31 --> Add32["Output X̃"]
  Add32 --> Add33["Output X̃"]
  Add33 --> Add34["Output X̃"]
  Add34 --> Add35["Output X̃"]
  Add35 --> Add36["Output X̃"]
  Add36 --> Add37["Output X̃"]
  Add37 --> Add38["Output X̃"]
  Add38 --> Add39["Output X̃"]
  Add39 --> Add40["Output X̃"]
  Add40 --> Add41["Output X̃"]
  Add41 --> Add42["Output X̃"]
  Add42 --> Add43["Output X̃"]
  Add43 --> Add44["Output X̃"]
  Add44 --> Add45["Output X̃"]
  Add45 --> Add46["Output X̃"]
  Add46 --> Add47["Output X̃"]
  Add47 --> Add48["Output X̃"]
  Add48 --> Add49["Output X̃"]
  Add49 --> Add50["Output X̃"]
  Add50 --> Add51["Output X̃"]
  Add51 --> Add52["Output X̃"]
  Add52 --> Add53["Output X̃"]
  Add53 --> Add54["Output X̃"]
  Add54 --> Add55["Output X̃"]
  Add55 --> Add56["Output X̃"]
  Add56 --> Add57["Output X̃"]
  Add57 --> Add58["Output X̃"]
  Add58 --> Add59["Output X̃"]
  Add59 --> Add60["Output X̃"]
  Add60 --> Add61["Output X̃"]
  Add61 --> Add62["Output X̃"]
  Add62 --> Add63["Output X̃"]
  Add63 --> Add64["Output X̃"]
  Add64 --> Add65["Output X̃"]
  Add65 --> Add66["Output X̃"]
  Add66 --> Add67["Output X̃"]
  Add67 --> Add68["Output X̃"]
  Add68 --> Add69["Output X̃"]
  Add69 --> Add70["Output X̃"]
  Add70 --> MultiHead
  MultiHead --> MultiHead
```
</details>

## Attention coefficients

$$
\mathbf {y} _ {n} = \sum_ {m = 1} ^ {N} a _ {n m} \mathbf {x} _ {m} \quad Y = A X
$$

$a_{nm}$ : how much token n attends to token m

$$
\begin{array}{l} a _ {n m} \geqslant 0 \\ \sum_ {m = 1} ^ {N} a _ {n m} = 1. \\ \end{array}
$$

This is a convex combination, play with one here:

https://www.geogebra.org/m/ekyrkytj

## How to compute attention weights

For each token $x_{i}$ we compute:

- A query $q_i$ . It encodes the kind of tokens that $x_i$ is willing to receive messages from.  
- A key $k_{i}$ . It encodes the kind of tokens that $x_{i}$ is willing to send messages to.  
- A value $v_i$ . It encodes the message that $x_i$ is willing to distribute to whomever wants to pay attention to it.

Now to compute $a_{nm}$ , the attention token n devotes to token m, we compute the dot product of $\langle q_{n}, k_{m} \rangle$ . The larger the dot product, the larger the attention.

## Attention via linear transformations

We can easily compute queries, keys and values using independent linear transformations of the matrix X.

![](images/8e88dfd4f71528a927a219ac0d3fdd2e7c906f688848822d2e5c94a3561fa3c5.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  Y --> mat_mul["mat mul"]
  mat_mul --> softmax["softmax"]
  softmax --> scale["scale"]
  scale --> mat_mul["mat mul"]
  mat_mul --> W_q["W^(q)"]
  mat_mul --> W_k["W^(k)"]
  mat_mul --> W_v["W^(v)"]
  W_q --> X["X"]
  W_k --> X
  W_v --> X
    style mat_mul fill:#f9f,stroke:#333
    style softmax fill:#ccf,stroke:#333
    style scale fill:#cfc,stroke:#333
    style W_q fill:#fcc,stroke:#333
    style W_k fill:#fcc,stroke:#333
    style W_v fill:#fcc,stroke:#333
```
</details>

$$
\mathbf {Q} = \mathbf {X} \mathbf {W} ^ {(q)}
$$

$$
\mathbf {K} = \mathbf {X} \mathbf {W} ^ {(k)}
$$

$$
\mathbf {V} = \mathbf {X} \mathbf {W} ^ {(v)}
$$

W matrices (encoding the linear transformations) are parameters of the Transformer layer

$$
A = \operatorname{Softmax} \left[ \frac {Q K ^ {T}}{\sqrt {D _ {k}}} \right]
$$

$$
Y = A V
$$

## Softmax

![](images/245ad4dbecf83be97b970491551e594e170ee40d621ea76742514ea5d48b80e3.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["0.25\n1.23\n-0.8"] --> B["Softmax"]
  B --> C["0.249\n0.664\n0.087"]
```
</details>

## Self-Attention

![](images/ae89ed8fe62341c0daeac4725feb6fe83c31c04b7aeac57e36130bf752e21f51.jpg)

<details>
<summary>bar chart</summary>

| Input        | Thinking | Machines |
| ------------ | -------- | -------- |
| Embedding    | X₁       | X₂       |
| Queries      | q₁       | q₂       |
| Keys         | k₁       | k₂       |
| Values       | v₁       | v₂       |
</details>

![](images/860a983842f2521dea978f42c77eaf7e8841c97cd7fe8b3ec8aa8e1e50a79d4c.jpg)

<details>
<summary>text_image</summary>

W^Q
W^K
W^V
</details>

![](images/e76a7a128868eccbf44a2265a4d42f5ef4a34a1b2604d65a5b604e605b252a37.jpg)

<details>
<summary>text_image</summary>

X
W^Q
=
Q
X
W^K
=
K
X
W^V
=
V
</details>

## Scaled self-attention in one slide. One attention head

![](images/c2f8c0f2a5dd82146902cfa8702ef5f95926757516adf89f461ef7a42c0b4a38.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  Y --> mat_mul["mat mul"]
  mat_mul --> softmax["softmax"]
  softmax --> scale["scale"]
  scale --> mat_mul["mat mul"]
  mat_mul --> W_q["W^(q)"]
  mat_mul --> W_k["W^(k)"]
  mat_mul --> W_v["W^(v)"]
  W_q --> X["X"]
  W_k --> X
  W_v --> X
    style mat_mul fill:#f9f,stroke:#333
    style softmax fill:#ccf,stroke:#333
    style scale fill:#cfc,stroke:#333
    style W_q fill:#ffc,stroke:#333
    style W_k fill:#ffc,stroke:#333
    style W_v fill:#ffc,stroke:#333
```
</details>

## Algorithm 12.1: Scaled dot-product self-attention

Input: Set of tokens $\mathbf{X} \in \mathbb{R}^{N \times D}: \{\mathbf{x}_1, \ldots, \mathbf{x}_N\}$ Weight matrices $\{\mathbf{W}^{(q)}, \mathbf{W}^{(k)}\} \in \mathbb{R}^{D \times D_k}$ and $\mathbf{W}^{(v)} \in \mathbb{R}^{D \times D_v}$

Output: Attention $(\mathbf{Q},\mathbf{K},\mathbf{V})\in \mathbb{R}^{N\times D_{\mathrm{v}}}$ : $\{\mathbf{y}_1,\dots ,\mathbf{y}_N\}$

$$
\mathbf {Q} = \mathbf {X W} ^ {(q)} / / \text {compute queries} \mathbf {Q} \in \mathbb {R} ^ {N \times D _ {k}}
$$

$$
\mathbf {K} = \mathbf {X W} ^ {(k)} / / \text {compute keys} \mathbf {K} \in \mathbb {R} ^ {N \times D _ {k}}
$$

$$
\mathbf {V} = \mathbf {X} \mathbf {W} ^ {(v)} / / \text {compute values} \mathbf {V} \in \mathbb {R} ^ {N \times D}
$$

return Attention(Q, K, V) = Softmax $\left[\frac{\mathbf{Q}\mathbf{K}^{\mathrm{T}}}{\sqrt{D_{\mathrm{k}}}}\right]\mathbf{V}$

## Multi head attention

![](images/49f11bc48c725c04319733e33305f533519c0c46de599e6eef631dcb8369d138.jpg)

<details>
<summary>tree diagram</summary>

| Word | Item |
| :--- | :--- |
| The_ | The |
| animal_ | animal |
| didn_ | didn |
| _ | _ |
| t_ | t |
| cross_ | cross |
| the_ | the |
| street_ | street |
| because_ | because |
| it_ | it |
| was_ | was |
| too_ | too |
| tire | tire |
| d_ | d |
</details>

## Multi head attention

## Each attention head learns a specific set of parameters W

![](images/52459a65adb510c227f51da0e679e7e03d69ae1e26ff0409533ba14cc585da0d.jpg)

<details>
<summary>text_image</summary>

I am specialized in colour information
I am specialized in taste information
I am specialized in movement information
I am specialized in time related information
</details>

Together, we can run the galaxy!

## Multi head attention

![](images/52212993dd4fbe55955ba91d21c23a30f1917aaec92573621905ba0d3419c299.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["linear"] --> B["concat"]
  B --> C["self-attention"]
  B --> D["self-attention"]
  B --> E["self-attention"]
  C --> F["X"]
  D --> F
  E --> F
  F --> G["..."]
  G --> H["X"]
  H --> I["Y"]
  I --> A
```
</details>

## Multi-Head Attention

1) This is our input sentence\*

2) We embed each word\*

3) Split into 8 heads. We multiply X or R with weight matrices

4) Calculate attention using the resulting Q/K/V matrices

5) Concatenate the resulting Z matrices, then multiply with weight matrix $W^{o}$ to produce the output of the layer

Thinking Machines

![](images/72903e9751e56cff5f49fe79575c380ae806817d66355ea403549d06b3335ab9.jpg)

\* In all encoders other than #0, we don't need embedding. We start directly with the output of the encoder right below this one

![](images/f4f10d5b28167b2cafa1598c32aec5050b5758bdbd9d81090e6c29a381e234db.jpg)

![](images/69572b8cac517119cf6aca6b24ab775ca0df273bd3bcf67d923815bf7209bf6c.jpg)

<details>
<summary>text_image</summary>

W₀^Q
W₀^K
W₀^V
</details>

![](images/f0aa80e8e0654174dd47167c599b1a467ef076e2d8c77c39d6791d21428d7d82.jpg)

![](images/e2382fa4fa6fbba1216fd72feae22b24136c87de570930bf0eb00411169f46ef.jpg)

![](images/b1134e7f8c0661f28e63bf121a50886f3dc0827d8fa443ecb0695cfed35e7d96.jpg)

<details>
<summary>text_image</summary>

W₁^Q
W₁^K
W₁^V
</details>

![](images/64dfe571cda09336cad1bec7a028df13ade80011949f932023f3aa5b84d1bf42.jpg)

![](images/fcc176ab29274dfd580ef8df6fe80dfb1bcd24fc2209a4962a872723096e67de.jpg)

![](images/09af7344786c994e74e49b827f2971f62a70fd72c405e1bb36f089a8283a26e3.jpg)

<details>
<summary>text_image</summary>

W7^Q
W7^K
W7^V
</details>

![](images/8be2578e27d300f8cd121ea209343c0b0cb8a03643b22a157ddbbe39df7f81c8.jpg)

<details>
<summary>text_image</summary>

Q₇
K₇
V₇
</details>

![](images/ce53ea75ff55dfc063a504351c6b5eb6b0fbb71400e65a3932aa07f872780ab3.jpg)

![](images/4a34a45d0b4bb95c6a75fa7123b05caf0fa84167f1271166dfe0d5cb563e3358.jpg)

<details>
<summary>text_image</summary>

W⁰
Z
</details>

## Multi head attention

## Algorithm 12.2: Multi-head attention

Input: Set of tokens $\mathbf{X} \in \mathbb{R}^{N \times D}: \{\mathbf{x}_1, \ldots, \mathbf{x}_N\}$

Query weight matrices $\{\mathbf{W}_1^{(\mathrm{q})},\dots ,\mathbf{W}_H^{(\mathrm{q})}\} \in \mathbb{R}^{D\times D}$

Key weight matrices $\{\mathbf{W}_1^{(\mathrm{k})},\dots ,\mathbf{W}_H^{(\mathrm{k})}\} \in \mathbb{R}^{D\times D}$

Value weight matrices $\{\mathbf{W}_1^{(\mathrm{v})},\dots ,\mathbf{W}_H^{(\mathrm{v})}\} \in \mathbb{R}^{D\times D_{\mathrm{v}}}$

Output weight matrix $\mathbf{W}^{(o)}\in \mathbb{R}^{HD_{v}\times D}$

Output: $\mathbf{Y} \in \mathbb{R}^{N \times D}: \{\mathbf{y}_1, \dots, \mathbf{x}_N\}$

// compute self-attention for each head (Algorithm 12.1)

for $h = 1,\dots ,H$ do

$$
\mathbf {Q} _ {h} = \mathbf {X} \mathbf {W} _ {h} ^ {\mathrm{(q)}}, \quad \mathbf {K} _ {h} = \mathbf {X} \mathbf {W} _ {h} ^ {\mathrm{(k)}}, \quad \mathbf {V} _ {h} = \mathbf {X} \mathbf {W} _ {h} ^ {\mathrm{(v)}}
$$

$$
\mathbf {H} _ {h} = \text { Attention } \left(\mathbf {Q} _ {h}, \mathbf {K} _ {h}, \mathbf {V} _ {h}\right) / / \mathbf {H} _ {h} \in \mathbb {R} ^ {N \times D _ {\mathrm{v}}}
$$

end for

$\mathbf{H} = \operatorname {Concat}\left[\mathbf{H}_{1},\dots ,\mathbf{H}_{\mathbf{N}}\right]$ // concatenate heads

return $\mathbf{Y}(\mathbf{X}) = \mathbf{H}\mathbf{W}^{(\mathrm{o})}$

## Transformer layer

## Algorithm 12.3: Transformer layer

Input: Set of tokens $X \in R^{N \times D} : \{x_{1}, \ldots, x_{N}\}$ Multi-head self-attention layer parameters
Feed-forward network parameters

Output: $\widetilde{\mathbf{X}}\in \mathbb{R}^{N\times D}:\{\widetilde{\mathbf{x}}_1,\dots ,\widetilde{\mathbf{x}}_N\}$

$\mathbf{Z} = \text{LayerNorm}[\mathbf{Y}(\mathbf{X}) + \mathbf{X}] // \mathbf{Y}(\mathbf{X})$ from Algorithm 12.2 $\widetilde{\mathbf{X}} = \text{LayerNorm}[\text{MLP}[\mathbf{Z}] + \mathbf{Z}] // \text{shared neural network}$ return $\widetilde{\mathbf{X}}$

Y(X): Result of applying multi-head self-attention

![](images/ecf72dd09f5c843e51891dd4504dfc8589f05a5e2a0596cdb1922161dc4a2562.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  X["Input X̃"] --> Add1["add & norm"]
  Add1 --> MLP["MLP"]
  MLP --> Add2["add & norm"]
  Add2 --> MultiHead["multi-head self-attention"]
  MultiHead --> Add3["add & norm"]
  Add3 --> Add4["MLP"]
  Add4 --> Add5["add & norm"]
  Add5 --> Add6["Output X̃"]
  Add6 --> Add7["Output X"]
  Add7 --> Add8["Output X̃"]
  Add8 --> Add9["Output X"]
  Add9 --> Add10["Output X̃"]
  Add10 --> Add11["Output X̃"]
  Add11 --> Add12["Output X̃"]
  Add12 --> Add13["Output X̃"]
  Add13 --> Add14["Output X̃"]
  Add14 --> Add15["Output X̃"]
  Add15 --> Add16["Output X̃"]
  Add16 --> Add17["Output X̃"]
  Add17 --> Add18["Output X̃"]
  Add18 --> Add19["Output X̃"]
  Add19 --> Add20["Output X̃"]
  Add20 --> Add21["Output X̃"]
  Add21 --> Add22["Output X̃"]
  Add22 --> Add23["Output X̃"]
  Add23 --> Add24["Output X̃"]
  Add24 --> Add25["Output X̃"]
  Add25 --> Add26["Output X̃"]
  Add26 --> Add27["Output X̃"]
  Add27 --> Add28["Output X̃"]
  Add28 --> Add29["Output X̃"]
  Add29 --> Add30["Output X̃"]
  Add30 --> Add31["Output X̃"]
  Add31 --> Add32["Output X̃"]
  Add32 --> Add33["Output X̃"]
  Add33 --> Add34["Output X̃"]
  Add34 --> Add35["Output X̃"]
  Add35 --> Add36["Output X̃"]
  Add36 --> Add37["Output X̃"]
  Add37 --> Add38["Output X̃"]
  Add38 --> Add39["Output X̃"]
  Add39 --> Add40["Output X̃"]
  Add40 --> Add41["Output X̃"]
  Add41 --> Add42["Output X̃"]
  Add42 --> Add43["Output X̃"]
  Add43 --> Add44["Output X̃"]
  Add44 --> Add45["Output X̃"]
  Add45 --> Add46["Output X̃"]
  Add46 --> Add47["Output X̃"]
  Add47 --> Add48["Output X̃"]
  Add48 --> Add49["Output X̃"]
  Add49 --> Add50["Output X̃"]
  Add50 --> Add51["Output X̃"]
  Add51 --> Add52["Output X̃"]
  Add52 --> Add53["Output X̃"]
  Add53 --> Add54["Output X̃"]
  Add54 --> Add55["Output X̃"]
  Add55 --> Add56["Output X̃"]
  Add56 --> Add57["Output X̃"]
  Add57 --> Add58["Output X̃"]
  Add58 --> Add59["Output X̃"]
  Add59 --> Add60["Output X̃"]
  Add60 --> Add61["Output X̃"]
  Add61 --> Add62["Output X̃"]
  Add62 --> Add63["Output X̃"]
  Add63 --> Add64["Output X̃"]
  Add64 --> Add65["Output X̃"]
  Add65 --> Add66["Output X̃"]
  Add66 --> Add67["Output X̃"]
  Add67 --> Add68["Output X̃"]
  Add68 --> Add69["Output X̃"]
  Add69 --> Add70["Output X̃"]
  Add70 --> MultiHead
  MultiHead --> MultiHead
```
</details>

## Masked transformer layer. A regular transformer layer but with masked attention.

How much token “Life” attends to token “first”

In the row for corresponding to “Life”, mask out all words that come after “Life”

![](images/96d8f12c860deacbbcd396e99c84da8fcfa153b629122e83f8b276a50ec2ca70.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Layer Norm"] --> B["+"]
  B --> C["Feed Forward"]
  C --> D["Layer Norm"]
  D --> E["+"]
  E --> F["Masked Multi Self Attention"]
  F --> D
  D --> G["Feedback Loop"]
  G --> H["Output"]
```
</details>

![](images/147a1c84885b179c6f43e9d2cf363e00ffdc0fd61728d4d348d5cfbce72c635c.jpg)

<details>
<summary>heatmap</summary>

| | Life | is | short | eat | desert | first |
|---|---|---|---|---|---|---|
| Life | 0.17 | 0.13 | 0.18 | 0.16 | 0.15 | 0.18 |
| is | 0.03 | 0.68 | 0.02 | 0.08 | 0.14 | 0.02 |
| short | 0.19 | 0.06 | 0.25 | 0.14 | 0.11 | 0.23 |
| eat | 0.15 | 0.21 | 0.14 | 0.16 | 0.17 | 0.14 |
| desert | 0.13 | 0.27 | 0.11 | 0.16 | 0.18 | 0.12 |
| first | 0.19 | 0.02 | 0.31 | 0.11 | 0.07 | 0.27 |
</details>

![](images/2367477a4ac6baae70edbe367527efd8cb3ecd9645ebf06fb5c13349377fd6f8.jpg)

<details>
<summary>heatmap</summary>

| | Life | is | short | eat | desert | first |
|---|---|---|---|---|---|---|
| Life | 0.17 | 0.13 | 0.18 | 0.16 | 0.15 | 0.18 |
| is | 0.03 | 0.68 | 0.02 | 0.08 | 0.14 | 0.02 |
| short | 0.19 | 0.06 | 0.25 | 0.14 | 0.11 | 0.23 |
| eat | 0.15 | 0.21 | 0.14 | 0.16 | 0.17 | 0.14 |
| desert | 0.13 | 0.27 | 0.11 | 0.16 | 0.18 | 0.12 |
| first | 0.19 | 0.02 | 0.31 | 0.11 | 0.07 | 0.27 |
</details>

Attention weights calculated in the previous "Self-Attention" section

## Cross-attention

![](images/93e812735abca8d5b12ac1845ed98926079605a776b1f5445f0489063e85f7b7.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Encoder"] --> B["Feed forward"]
  A --> C["Multi-head attention"]
  A --> D["Masked multi-head attention"]
  B --> E["Add & norm"]
  C --> F["add & norm"]
  D --> G["add & norm"]
  E --> H["Add & norm"]
  F --> I["Add & norm"]
  G --> J["Masked multi-head attention"]
  H --> K["Feed forward"]
  I --> L["Multi-head attention"]
  J --> M["Masked multi-head attention"]
    style A fill:#f9f,stroke:#333
    style B fill:#bbf,stroke:#333
    style C fill:#bfb,stroke:#333
    style D fill:#ffb,stroke:#333
    style E fill:#fff,stroke:#333
    style F fill:#fff,stroke:#333
    style G fill:#fff,stroke:#333
    style H fill:#fff,stroke:#333
    style I fill:#fff,stroke:#333
    style J fill:#fff,stroke:#333
    style K fill:#fff,stroke:#333
    style L fill:#fff,stroke:#333
```
</details>

## Cross-Attention (e.g. Machine Translation)

![](images/c822782b25a88fb4f8278fc6a435be73f922a48149bcd2bd2d9cc1b7cc64d35e.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Start"] --> B["Student"]
  B --> C["Intermediate Layer"]
  C --> D["Student"]
  D --> E["Intermediate Layer"]
  E --> F["Student"]
  F --> G["Intermediate Layer"]
  G --> H["Student"]
  H --> I["Intermediate Layer"]
  I --> J["Student"]
  J --> K["Intermediate Layer"]
  K --> L["Student"]
  L --> M["Intermediate Layer"]
  M --> N["Student"]
  N --> O["Intermediate Layer"]
  O --> P["Student"]
  P --> Q["Intermediate Layer"]
  Q --> R["Student"]
  R --> S["Intermediate Layer"]
  S --> T["Student"]
  T --> U["Intermediate Layer"]
  U --> V["Student"]
  V --> W["Intermediate Layer"]
  W --> X["Student"]
  X --> Y["Intermediate Layer"]
  Y --> Z["Student"]
  Z --> AA["Intermediate Layer"]
  AA --> AB["Student"]
  AB --> AC["Intermediate Layer"]
  AC --> AD["Student"]
  AD --> AE["Intermediate Layer"]
  AE --> AF["Student"]
  AF --> AG["Intermediate Layer"]
  AG --> AH["Student"]
  AH --> AI["Intermediate Layer"]
  AI --> AJ["Student"]
  AJ --> AK["Intermediate Layer"]
  AK --> AL["Student"]
  AL --> AM["Intermediate Layer"]
  AM --> AN["Student"]
  AN --> AO["Intermediate Layer"]
  AO --> AP["Student"]
  AP --> AQ["Intermediate Layer"]
  AQ --> AR["Student"]
```
</details>

## Cross-attention transformer layer

Tokens entering through
here determine the
questions

Tokens entering through here determine the keys and the messages (values)

![](images/4cb66e853a2cc2056cf891384c307ce1741939fe11bf5144cba803a4f62e937f.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
    subgraph Inputs_1[Inputs 1]
  A["x₁"] -->|d| B["W_q"]
  B -->|d_q| C["Q"]
  C -->|n| D["K"]
  D -->|m| E["QKᵀ"]
  E --> F["Softmax"]
    end

    subgraph Inputs_2[Inputs 2]
  A["x₂"] -->|d| B["W_k"]
  B -->|d_k = d_q| C["K"]
  C -->|m| D["V"]
  D -->|m| E["V"]
  E -->|m| F["A"]
  F --> G["Z"]
  G --> H["Outputs"]

    style Inputs_1 fill:#f9f,stroke:#333
    style Inputs_2 fill:#f9f,stroke:#333
    style Outputs fill:#ccf,stroke:#333
    style Softmax fill:#cfc,stroke:#333
    style A fill:#fcc,stroke:#333
    style B fill:#cff,stroke:#333
    style C fill:#ffc,stroke:#333
    style D fill:#cfc,stroke:#333
    style E fill:#fcc,stroke:#333
    style F fill:#cfc,stroke:#333
    style G fill:#cfc,stroke:#333
    style H fill:#fcc,stroke:#333
```
</details>

![](images/059a022f8cb22dd05678e61f3ed7e267405cb7cf54888aea865e453531f1f43d.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["add & norm"] --> B["MLP"]
  B --> C["add & norm"]
  C --> D["multi-head cross-attention"]
  D --> E["add & norm"]
  E --> F["masked multi-head self-attention"]
  F --> G["X̃"]
  G --> H["Z"]
  H --> I["K"]
  I --> J["V"]
  J --> K["Q"]
  K --> D
    style A fill:#f9f,stroke:#333
    style B fill:#f9f,stroke:#333
    style C fill:#f9f,stroke:#333
    style D fill:#ff0,stroke:#333
    style E fill:#ff0,stroke:#333
    style F fill:#ff0,stroke:#333
    style G fill:#fff,stroke:#333
    style H fill:#fff,stroke:#333
    style I fill:#fff,stroke:#333
    style J fill:#fff,stroke:#333
    style K fill:#fff,stroke:#333
  style_L["X"] --> M["add & norm"]
  M --> N["Masked multi-head self-attention"]
```
</details>

## Transformer & Multi-Head Attention

![](images/50a24f1ed4141de3a60b0446414d49d8043dbfe9847e818a86168b876d6e3cd7.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Inputs"] --> B["Input Embedding"]
  B --> C["Add & Norm"]
  C --> D["Feed Forward"]
  D --> E["Multi-Head Attention"]
  E --> F["Add & Norm"]
  F --> G["Masked Multi-Head Attention"]
  G --> H["Add & Norm"]
  H --> I["Feed Forward"]
  I --> J["Add & Norm"]
  J --> K["Softmax"]
  K --> L["Linear"]
  L --> M["Output Probabilities"]
    style A fill:#f9f,stroke:#333
    style B fill:#ccf,stroke:#333
    style C fill:#cfc,stroke:#333
    style D fill:#fcc,stroke:#333
    style E fill:#cff,stroke:#333
    style F fill:#ffc,stroke:#333
    style G fill:#cfc,stroke:#333
    style H fill:#fcc,stroke:#333
    style I fill:#cfc,stroke:#333
    style J fill:#fcc,stroke:#333
    style K fill:#cfc,stroke:#333
    style L fill:#fcc,stroke:#333
    style M fill:#cfc,stroke:#333
  style_N["Positional Encoding"] --> O["Add & Norm"]
  O --> P["Multi-Head Attention"]
  P --> Q["Add & Norm"]
  Q --> R["Masked Multi-Head Attention"]
  R --> S["Add & Norm"]
  S --> T["Feed Forward"]
  T --> U["Add & Norm"]
  U --> V["Softmax"]
  V --> W["Nx"]
    style N fill:#f9f,stroke:#333
    style O fill:#ccf,stroke:#333
    style P fill:#cfc,stroke:#333
    style Q fill:#fcc,stroke:#333
    style R fill:#cfc,stroke:#333
    style S fill:#fcc,stroke:#333
    style T fill:#cfc,stroke:#333
    style U fill:#fcc,stroke:#333
```
</details>

Figure 1: The Transformer - model architecture.

## Transformers vs. RNNs

<table><tr><td>Challenges with RNNs</td><td>Transformers</td></tr><tr><td>Long range dependenciesGradient vanishing and explosionLarge # of training stepsSequential/recurrence → can’t parallelizeComplexity per layer: O(n*d2)</td><td>Can model long-rangedependenciesNo gradient vanishing and explosionFewer training stepsCan parallelize computation!Complexity per layer: O(n2*d)</td></tr></table>

## Large Language Models

▶ Scaled up versions of Transformer architecture, e.g. millions/billions of parameters  
▶ Typically trained on massive amounts of “general” textual data (e.g. web corpus)  
▶ Training objective is typically “next token prediction”: $P(W_{t+1}|W_t, W_{t-1}, \ldots, W_1)$  
Emergent abilities as they scale up (e.g. chain-of-thought reasoning)  
▶ Heavy computational cost (time, money, GPUs)  
▶ Larger general ones: “plug-and-play” with few or zero-shot learning  
▶ Train once, then adapt to other tasks without needing to retrain  
▶ E.g. in-context learning and prompting

## Emergent Abilities of Large Language Models

▶ Why do LLMs work so well? What happens as you scale up?  
▶ Potential explanation: emergent abilities!  
▶ An ability is emergent if it is present in larger but not smaller models  
▶ Not have been directly predicted by extrapolating from smaller models  
▶ Performance is near-random until a certain critical threshold, then improves heavily  
▶ Known as a “phase transition” and would not have been extrapolated

## Few-Shot Prompting

![](images/b837a60dc28d642ce58472eda85627f917a5480cb2e46db944237478cad02de7.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["Input"] --> B["Review: This movie sucks.<br>Sentiment: negative.<br>Review: I love this movie.<br>Sentiment:"]
  B --> C["Language model"]
  C --> D["Output<br>positive."]
```
</details>

Figure 1: Example of an input and output for few-shot prompting.

![](images/fa40166bf97db3f13e1b0cd1ee5eb011d5b20378269d12c0ec5349203ad5ece4.jpg)

![](images/fdff2cabf011a9357a9e398a0e56e254c2a99ce609359af3a8a6a8a317f546e1.jpg)

<details>
<summary>line chart</summary>

| x        | Accuracy (%) |
| -------- | ------------ |
| 10^18    | 0            |
| 10^20    | 0            |
| 10^22    | 0            |
| 10^24    | 35           |
</details>

![](images/351a1f27ac8419bcd101723d6106528e3098be0a55a7b563f8c933c47071cbec.jpg)

<details>
<summary>line chart</summary>

| X (log scale) | BLEU (%) |
| ------------- | -------- |
| 10^18         | 0        |
| 10^20         | 0        |
| 10^22         | 0        |
| 10^24         | 25       |
</details>

![](images/6f157c55b39e822c807b2df1b35aa2adb37b0fac088dece37a18d6f71f3352e7.jpg)

<details>
<summary>line chart</summary>

| x        | Exact match (%) |
| -------- | --------------- |
| 10^18    | 0               |
| 10^20    | 0               |
| 10^22    | 0               |
| 10^24    | 15              |
</details>

![](images/0321a7b63966ea73c83e82e54f535ffda82b4bf6b396bfdf9b904d5b089193cc.jpg)

<details>
<summary>line chart</summary>

| x        | Exact match (%) |
| -------- | --------------- |
| 10^18    | 25              |
| 10^20    | 23              |
| 10^22    | 24              |
| 10^24    | 45              |
</details>

![](images/4ddc32fc9e3cf9392c50a616731d987c2bc9f7e07d64b72b8993a540ba35a0d0.jpg)

<details>
<summary>line chart</summary>

| x      | Accuracy (%) |
| ------ | ------------ |
| 10^20  | 20           |
| 10^22  | 20           |
| 10^24  | 45           |
</details>

![](images/8d062ccfe7931d5a4d422d0e0ed777917c5ebde5a821bc01f78f0d348be4f47c.jpg)

<details>
<summary>line chart</summary>

| x        | Accuracy (%) |
| -------- | ------------ |
| 10^20    | 10           |
| 10^21    | 8            |
| 10^22    | 15           |
| 10^23    | 45           |
</details>

![](images/5ced88e6ff6cf29cc9fe4135723d811ed4d41410bf42e1db9c9cbc7c8a196b5f.jpg)

<details>
<summary>line chart</summary>

| x      | Accuracy (%) |
| ------ | ------------ |
| 10^20  | 25           |
| 10^22  | 30           |
| 10^24  | 60           |
</details>

![](images/e23934dd676f648eef7c4b7d665356ddf0ff9441d3d73280bd4927038084a35e.jpg)

<details>
<summary>line chart</summary>

| Word in context | Accuracy (%) |
| --------------- | ------------ |
| 10^20           | 50           |
| 10^22           | 48           |
| 10^24           | 65           |
</details>

Model scale (training FLOPs)  
Figure 2: Eight examples of emergence in the few-shot prompting setting. Each point is a separate model. The ability to perform a task via few-shot prompting is emergent when a language model achieves random performance until a certain scale, after which performance significantly increases to well-above random. Note that models that used more training compute also typically have more parameters—hence, we show an analogous figure with number of model parameters instead of training FLOPs as the x-axis in Figure 11. A–D: BIG-Bench (2022), 2-shot. E: Lin et al. (2021) and Rae et al. (2021). F: Patel & Pavlick (2022). G: Hendrycks et al. (2021a), Rae et al. (2021), and Hoffmann et al. (2022). H: Brown et al. (2020), Hoffmann et al. (2022), and Chowdhery et al. (2022) on the WiC benchmark (Pilehvar & Camacho-Collados, 2019).

## Potential Explanations of Emergence

▶ Currently few explanations for why these abilities emerge  
▶ Evaluation metrics used to measure these abilities may not fully explain why they emerge  
▶ Disclaimer: maybe emergent abilities of LLMs are a mirage!!!

https://arxiv.org/abs/2304.15004  
▶ “Emergent abilities appear due to the researcher's choice of metric rather than due to fundamental changes in model behavior with scale”

## Reinforcement Learning with Human Feedback (RLHF)

▶ RLHF: technique that trains a "reward model" directly from human feedback  
▶ Uses the model as a reward function to optimize an agent's policy using reinforcement learning (RL) through an optimization algorithm  
▶ Ask humans to rank instances of the agent's behavior, e.g. which produced response is better

![](images/5f0bac179388fe35f89295792c7d51a94ccbd1f82a08448f7cc8dd6161022cf1.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["RL ALGORITHM"] -->|PREDICTED REWARD| B["REWARD PREDICTOR"]
  B -->|OBSERVATION| C["ENVIRONMENT"]
  C -->|ACTION| A
  D["HUMAN FEEDBACK"] -->|HUMAN FEEDBACK| B
```
</details>

## Direct Preference Optimization (DPO)

![](images/d582436b10798c587a9bb9d981a332ffe353cf5509ec6ad9d4dd91ca28ae93bc.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["preference data"] --> B["maximum likelihood"]
  B --> C["reward model"]
  C --> D["label rewards"]
  D --> E["sample completions"]
  E --> F["LM policy"]
    style A fill:#f9f,stroke:#333
    style F fill:#bbf,stroke:#333
    subgraph Reinforcement_learning_from_Human_Feedback_RLHF["\"Reinforcement learning from Human Feedback (RLHF)\""]
        G["write me a poem about the history of jazz"]
    end
```
</details>

![](images/dc7b568a9c505290f0b3496d7317a6f0e406a798d855c0fbc183e9caca721970.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["preference data"] --> B["> y_w"]
  B --> C["maximum likelihood"]
  C --> D["final LM"]
```
</details>

Figure 1: DPO optimizes for human preferences while avoiding reinforcement learning. Existing methods for fine-tuning language models with human feedback first fit a reward model to a dataset of prompts and human preferences over pairs of responses, and then use RL to find a policy that maximizes the learned reward. In contrast, DPO directly optimizes for the policy best satisfying the preferences with a simple classification objective, without an explicit reward function or RL.

## GPT-4

▶ Supervised learning on large dataset, then RLHF and RLAIF  
▶ GPT-4 trained on both images and text, vision is also out!  
▶ Discuss humor in images, summarize screenshot text, etc.  
▶ GPT-4 is "more reliable, creative, and able to handle much more nuanced instructions than GPT-3.5"  
▶ Much longer context windows of 8,192 and 32,768 tokens  
▶ Does exceptionally well on standardized tests  
▶ Did not release technical details of GPT-4

![](images/c402beda3582959721fdc5cc438323e623372ad23bdf7edeec9d03ba61953e5f.jpg)

<details>
<summary>natural_image</summary>

Close-up of a humanoid robot head with exposed mechanical components and a circular dial (no text or symbols visible)
</details>

GPT-4

## Gemini

▶ Latest: Gemini 1.5 Pro  
Gemini Ultra performs better than ChatGPT on 30 of the 32 academic benchmarks in reasoning and understanding it tested on.  
▶ Effectively processes and integrates data from diff modalities:  
▶ Text, audio, image, video  
▶ Based on a Mixture-of-Experts (MoE) model  
▶ Significantly improves efficiency in training and application

## Gemini

▶ Based on a Mixture-of-Experts (MoE) model

Combination of multiple small Neural networks known as ‘Experts’ which are trained and capable of handling particular data and performing specialised tasks.  
▶ 'Gating network' which predicts which response is best suited to address the request.

![](images/c00c504158545a02d0f4148ae2467b26fec2520bee1b8aa9e46ce2800bac2b50.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Input"] --> B["Text Expert"]
  A --> C["Image Expert"]
  A --> D["Fusion Expert"]
  B --> E["Output"]
  C --> E
  D --> E
  E --> F["Gating Network"]
  F --> A
    style A fill:#f9f,stroke:#333
    style E fill:#bbf,stroke:#333
    style F fill:#dfd,stroke:#333
    note1["EXPERTS"]
    note2["Gating Network"]
    note3["30%"]
    note4["60%"]
    note5["10%"]
    note6["10%"]
    note7["30%"]
```
</details>

Source link

## Where we are (2024)

## Recently Taken Off:

- LLM boom: ChatGPT, GPT-4, Gemini, open-source models  
• Human alignment and interaction

\- Reinforcement learning & human feedback

• Controlling toxicity, bias, and ethics  
- More use in unique applications: audio, art/music, neuro/bio, coding, games, physical tasks, etc.  
- Other: diffusion models (e.g. text-to-image/video gen)

○ Also, Diffusion Transformer (DiT)

![](images/1a2b90ad55c64b09122ca6c476c054855401bf4dcc7c22d27e44a7b0ce17224f.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Examples"] --> B["&quot;Explain quantum computing in simple terms&quot; →"]
  C["Capabilities"] --> D["Remembers what user said earlier in the conversation"]
  E["Limitations"] --> F["May occasionally generate incorrect information"]
  G["&quot;Got any creative ideas for a 10 year old's birthday?&quot; →"] --> H["Allows user to provide follow-up corrections"]
  I["&quot;How do I make an HTTP request in Javascript?&quot; →"] --> J["Trained to decline inappropriate requests"]
  K["Limited knowledge of world and events after 2021"] --> L["May occasionally produce harmful instructions or biased content"]
```
</details>

Image source: https://openai.com/blog/chatgpt/

![](images/825953621efc0ba6f0ad437d2d3234a9c746e140a3c0057d61d1b70c48f60e6c.jpg)

<details>
<summary>text_image</summary>

TEXT PROMPT an armchair in the shape of an avocado, an armchair imitating an avocado.
AI-GENERATED IMAGES
</details>

## The Future (What's Next?)

\- Can enable a lot more applications:

○ Generalist Agents  
- Longer video understanding and generation, finance + business  
- Incredibly long sequence modeling (GPT authors a novel)  
○ Domain-specific “Foundation models” - DoctorGPT, LawyerGPT, ...  
- Potential real-world impacts:

■ Personalized education and tutoring systems  
■ Advanced healthcare diagnostics, environmental monitoring & protection, etc.  
■ Real-time multilingual communication  
■ Interactive entertainment & gaming (e.g. NPCs)

![](images/625d268578d1bb973993688050a8fc585e0d94d1df5aa19aeee4cf2a4b9ff87a.jpg)

<details>
<summary>natural_image</summary>

Futuristic futuristic spaceship with reflective surface and dynamic motion blur (no text or symbols)
</details>

## The Future (What's Missing?)

## • Missing Ingredients (to AGI/ASI?):

○ Reducing computation complexity  
- Enhanced human controllability  
○ Alignment with language models of human brain  
○ Adaptive learning and generalization across domains  
○ Multi-sensory multimodal embodiment (e.g. intuitive physics and commonsense)  
- Infinite/external memory: like Neural Turing Machines  
- Infinite/constant self-improvement and self-reflection capabilities  
○ Complete autonomy and long-horizon decision-making  
○ Emotional intelligence and social understanding  
- Ethical reasoning and value alignment

![](images/cce726e250d0bbb6b826d7607019f1984a01b5317791963c52021cbfb0d89dcd.jpg)

<details>
<summary>natural_image</summary>

Futuristic futuristic spaceship with reflective surface and dynamic control panels (no text or symbols visible)
</details>

## Questions?

![](images/534b6861aa23c0713072445f0ee117140f4eabe3aa46ebf0ebffd52e965dcd0c.jpg)

<details>
<summary>text_image</summary>

ASK NO QUESTIONS, HEAR NO LIES
M.EM
</details>

## Hugging face

https://medium.com/@anthony.demeusy/introduction-to-hugging-face-a-starters-guide-to-using-online-models-d7d7923a9aa5

## Transformers and LLMs: An Introduction

## Challenges and Weaknesses

## Challenges of NLP:

▶ Discrete nature of text  
▶ More difficult data augmentation  
▶ Text is “precise” - one wrong word changes entire meaning of a sentence  
▶ Potential for long context lengths and memories (e.g. conversations)  
▶ Many more...

## Weaknesses of earlier models/apporoaches:

▶ Short context length  
▶ “Linear” reasoning - no attention mechanism to focus on other parts  
▶ Earlier approaches (e.g. word2vec) do not adapt based on context

## NLP Throughout the Years

## Rule Based NLP Systems

Welcome to

<table><tr><td>EEEEEE</td><td>LL</td><td>III</td><td>ZZZZZZ</td><td colspan="2">AAAAA</td></tr><tr><td>EE</td><td>LL</td><td>II</td><td>ZZ</td><td>AA</td><td>AA</td></tr><tr><td>EEEE</td><td>LL</td><td>II</td><td>ZZZ</td><td colspan="2">AAAAAAAA</td></tr><tr><td>EE</td><td>LL</td><td>II</td><td>ZZ</td><td>AA</td><td>AA</td></tr><tr><td>EEEEEE</td><td>LLLLLLL</td><td>III</td><td>ZZZZZZ</td><td>AA</td><td>AA</td></tr></table>

Eliza is a mock Rogerian psychotherapist.

The original program was described by Joseph Weizenbaum in 1966.

This implementation by Norbert Landsteiner 2005.

ELIZA: Is something troubling you?

YOU: Men are all alike.

ELIZA: What is the connection, do you suppose?

YOU: They're always bugging us about something or other.

ELIZA: Can you think of a specific example?

YOU: Well, my boyfriend made me come here.

ELIZA: Is it important to you that your boyfriend made you come here?

YOU: He says I'm depressed much of the time.

ELIZA: I am sorry to hear that you are depressed.

YOU: It's true. I am unhappy.

ELIZA: Can you explain what made you unhappy ?

YOU:

## Linguistic Foundations

▶ Rule-based approaches  
▶ Semantic parsing  
▶ Analyzing linguistic structure and grammars of text

SIMPLE RULE BASED RULE  
![](images/e0c06d0214c073ac9e4cdbf440ea11d71e0987a3b9c7c3813a46c84d0f393efb.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Start"] --> B["Input phrase"]
  B --> C{Is phrase="take call"}
  C -->|NO| D{Is phrase="accept call"}
  D -->|NO| E{Is phrase="join call"}
  E -->|NO| F["Could not understand phrase"]
  E -->|YES| G["Execute &quot;accept call&quot;"]
  G --> H["Stop"]
  C -->|YES| G
```
</details>

![](images/5a747933e13b7c594fd0242bc102ea6b2577679854757931e24455ab2c70194e.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["root"] --> B["ndobj"]
  A --> C["nsubj"]
  B --> D["det"]
  B --> E["nmod"]
  D --> F["I prefer the morning flight through Denver"]
  D --> G["nmod"]
  E --> H["case"]
```
</details>

![](images/eb8c06d4071407a25dc2264c23dba3389db906b0136d8b7935d97e6716d2a4f4.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  S --> NP
  S --> VP
  NP --> Pro
  VP --> Verb
  VP --> NP
  NP --> Det
  Det --> the
  NP --> Nom
  Nom --> Nom
  Nom --> Noun
  Nom --> P
  Nom --> PP
  PP --> Noun
  PP --> P
  PP --> NP
  Noun --> morning
  flight --> morning
  through --> morning
  Pro --> Denver
```
</details>

## Word Embeddings

▶ Represent each word as a “vector” of numbers  
▶ Converts a “discrete” representation to “continuous”, allowing for:  
▶ More "fine-grained" representations of words  
▶ Useful computations such as cosine/eucl distance  
▶ Visualization and mapping of words onto a semantic space

Examples:

▶ Word2Vec (2013), GloVe, BERT, ELMo

![](images/fc7ed07e7698fd2b340d977afa260f116a91dd6390402d181f24ab256124bd2c.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["man"] --> B["woman"]
  A --> C["king"]
  A --> D["queen"]
  E["cat"] --> F["dog"]
  F --> G["dogs"]
  H["France"] --> I["Paris"]
  I --> J["London"]
  J --> K["Rome"]
  L["England"] --> M["Italy"]
  N["mother"] --> O["daughter"]
  P["father"] --> Q["boy"]
  R["girl"] --> S["son"]
  T["she"] --> U["hisself"]
  V["longer"] --> W["long"]
  X["fast"] --> Y["fastest"]
  Z["slow"] --> AA["slower"]
  AB["fast"] --> AC["longer"]
  AD["longest"] --> AE["longest"]
```
</details>

## Seq2seq Models

▶ Recurrent Neural Networks (RNNs)  
▶ Long Short-Term Memory Networks (LSTMs)  
▶ "Dependency" and info between tokens  
▶ Gates to “control memory” and flow of information

![](images/a3d99cf679a6743a7a404175033210ae240385dd784f7db0543c056b7cf68033.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["ct-1"] --> B["X"]
  B --> C["+"]
  C --> D["tanh"]
  D --> E["X"]
  E --> F["ot"]
  F --> G["ct"]
  H["ht-1"] --> I["σ"]
  I --> J["ft"]
  J --> K["σ"]
  K --> L["it"]
  L --> M["σ"]
  M --> N["tanh"]
  N --> O["ot"]
  O --> P["tanh"]
  P --> Q["ct"]
  R["xt"] --> S["σ"]
  S --> T["ct-1"]
  U["yt"] --> V["Ct"]
  W["yt"] --> X["Ct-1"]
```
</details>

![](images/92bfb81b202b5e33df57e2303223cb9bbd0302b12cd9f09b3913ecf0f358d075.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["O"] -->|W| B["h"]
  C["X"] -->|U| B
  B --> D["..."]
  D -->|V| E["h_{t-1}"]
  E -->|W| F["h_t"]
  F -->|U| G["h_{t+1}"]
  G -->|W| H["..."]
    style A fill:#f9f,stroke:#333
    style C fill:#f9f,stroke:#333
    style B fill:#bbf,stroke:#333
    style E fill:#bbf,stroke:#333
    style F fill:#bbf,stroke:#333
    style G fill:#bbf,stroke:#333
    style H fill:#f9f,stroke:#333
```
</details>

## Attention and Transformers

- Allows to "focus attention" on particular aspects of the input text  
▶ Done by using a set of parameters, called "weights," that determine how much attention should be paid to each input at each time step  
These weights are computed using a combination of the input and the current hidden state of the model  
▶ Attention weights are computed (dot product of the query, key and value matrix), then a softmax function is applied to the dot product

$$
\text { attention } (Q, K, V) = \text { softmax } \left(\frac {Q K ^ {T}}{\sqrt {d _ {k}}}\right) V
$$

![](images/6802a3159043b044487c5166404667119076208e0a5c5b1cd363a216bf9902be.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["The"] --> B["animal"]
  A --> C["didn"]
  A --> D["'"]
  A --> E["t'"]
  A --> F["cross"]
  A --> G["the"]
  A --> H["street"]
  A --> I["because"]
  A --> J["it'"]
  A --> K["was'"]
  A --> L["too'"]
  A --> M["tire"]
  A --> N["d'"]
  O["The"] --> P["animal"]
  O --> Q["didn"]
  O --> R["'"]
  O --> S["t'"]
  O --> T["cross"]
  O --> U["the"]
  O --> V["street"]
  O --> W["because"]
  X["it'"] --> Y["was'"]
  X --> Z["too'"]
  X --> AA["tire"]
  X --> AB["d"]
```
</details>

## Analogy for Q, K, V

▶ Library system  
Imagine you're looking for information on a specific topic (query)  
▶ Each book in the library has a summary (key) that helps identify if it contains the information you're looking for  
▶ Once you find a match between your query and a summary, you access the book to get the detailed information (value) you need  
▶ Here, in Attention, we do a “soft match” across multiple values, e.g. get info from multiple books (“book 1 is most relevant, then book 2, then book 3, etc.”)

$$
\text { attention } (Q, K, V) = \text { softmax } \left(\frac {Q K ^ {T}}{\sqrt {d _ {k}}}\right) V
$$

## Attention and Transformers

▶ Attention weights used to compute the context vector, which is a weighted sum of the input at different positions  
▶ Context vector is used to update the hidden state of the model, which is used to generate the final output  
▶ "Pay attention" to different parts of the input, depending on the task at hand → more accurate and natural-sounding output, esp. when working with longer inputs (e.g. paragraphs)

![](images/18bef3ed047b2ef4e84084e4c3da3b71a634dc14eda9c52945d6cbf6dbc68f15.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Inputs"] --> B["Input Embedding"]
  B --> C["Add & Norm"]
  C --> D["Feed Forward"]
  D --> E["Add & Norm"]
  E --> F["Multi-Head Attention"]
  F --> G["Masked Multi-Head Attention"]
  G --> H["Output Embedding"]
  H --> I["Outputs (shifted right)"]
  I --> J["Positional Encoding"]
  J --> K["Add & Norm"]
  K --> L["Feed Forward"]
  L --> M["Add & Norm"]
  M --> N["Multi-Head Attention"]
  N --> O["Masked Multi-Head Attention"]
  O --> P["Output Embedding"]
  P --> Q["Positional Encoding"]
  Q --> R["Add & Norm"]
  R --> S["Feed Forward"]
  S --> T["Add & Norm"]
  T --> U["Multi-Head Attention"]
  U --> V["Masked Multi-Head Attention"]
  V --> W["Output Embedding"]
  W --> X["Positional Encoding"]
  X --> Y["Add & Norm"]
  Y --> Z["Feed Forward"]
  Z --> AA["Add & Norm"]
  AA --> AB["Multi-Head Attention"]
  AB --> AC["Masked Multi-Head Attention"]
  AC --> AD["Output Embedding"]
  AD --> AE["Positional Encoding"]
  AE --> AF["Add & Norm"]
  AF --> AG["Feed Forward"]
  AG --> AH["Add & Norm"]
  AH --> AI["Multi-Head Attention"]
  AI --> AJ["Masked Multi-Head Attention"]
  AJ --> AK["Output Embedding"]
  AK --> AL["Positional Encoding"]
  AL --> AM["Add & Norm"]
  AM --> AN["Feed Forward"]
  AN --> AO["Add & Norm"]
  AO --> AP["Multi-Head Attention"]
  AP --> AQ["Masked Multi-Head Attention"]
  AQ --> AR["Output Embedding"]
  AR --> AS["Positional Encoding"]
  AS --> AT["Add & Norm"]
  AT --> AU["Feed Forward"]
  AU --> AV["Add & Norm"]
  AV --> AW["Multi-Head Attention"]
  AW --> AX["Masked Multi-Head Attention"]
  AX --> AY["Output Embedding"]
  AY --> AZ["Positional Encoding"]
  AZ --> BA["Add & Norm"]
  BA --> BB["Feed Forward"]
  BB --> BC["Add & Norm"]
  BC --> BD["Multi-Head Attention"]
  BD --> BE["Masked Multi-Head Attention"]
  BE --> BF["Output Embedding"]
  BF --> BG["Positional Encoding"]
  BG --> BH["Add & Norm"]
  BH --> BI["Feed Forward"]
  BI --> BJ["Add & Norm"]
  BJ --> BK["Multi-Head Attention"]
  BK --> BL["Masked Multi-Head Attention"]
  BL --> BM["Output Embedding"]
  BM --> BN["Positional Encoding"]
  BN --> BO["Add & Norm"]
  BO --> BP["Feed Forward"]
  BP --> BQ["Add & Norm"]
  BQ --> BR["Multi-Head Attention"]
  BR --> BS["Masked Multi-Head Attention"]
  BS --> BT["Output Embedding"]
  BT --> BU["Positional Encoding"]
  BU --> BV["Add & Norm"]
  BV --> BW["Feed Forward"]
  BW --> BX["Add & Norm"]
  BX --> BY["Multi-Head Attention"]
  BY --> BZ["Masked Multi-Head Attention"]
  BZ --> CA["Output Embedding"]
```
</details>

Figure 1: The Transformer - model architecture.

## Self-Attention

![](images/93f9c433453f1ec79973c5399ce16b4ad4186eba155c1a3a7e7442443017d253.jpg)

<details>
<summary>bar chart</summary>

| Input        | Thinking | Machines |
| ------------ | -------- | -------- |
| Embedding    | X₁       | X₂       |
| Queries      | q₁       | q₂       |
| Keys         | k₁       | k₂       |
| Values       | v₁       | v₂       |
</details>

![](images/17da9fad79fefffb9cd38ba93047d21294e14ecf7cf7679255d4d19d9c0b9c68.jpg)

<details>
<summary>text_image</summary>

W^Q
W^K
W^V
</details>

![](images/a700ef95641afcb369c0b40d264c07511bc08a135d7a059936170d0e5f224063.jpg)

<details>
<summary>text_image</summary>

X
W^Q
=
Q
X
W^K
=
K
X
W^V
=
V
</details>

## Multi-Head Attention

1) This is our input sentence\*

2) We embed each word\*

3) Split into 8 heads. We multiply X or R with weight matrices

4) Calculate attention using the resulting Q/K/V matrices

5) Concatenate the resulting Z matrices, then multiply with weight matrix $W^{o}$ to produce the output of the layer

Thinking Machines

![](images/1332b266b903125a7f2a41324c6660925162d375e809a88ff62dfc21ff481c25.jpg)

\* In all encoders other than #0, we don't need embedding. We start directly with the output of the encoder right below this one

![](images/a568f08bafc9c8eef30457765c8b697f19a25f70b458eceb2b520322d74a1a2c.jpg)

![](images/1845c86be180cd5688d23b2795f81152a8567c8683d20c314b610ff4537317a1.jpg)

<details>
<summary>text_image</summary>

W₀^Q
W₀^K
W₀^V
</details>

![](images/1e456144df809cc35c51c6338e261f6e0ec63cdf38f3d9d3397b8f877b1a3a9b.jpg)

![](images/ba59c58da64581f56b15062930d2e13b55154c55c7549d67db0de1e0e8fe606c.jpg)

![](images/b7692b00050a88f9013d185b7b8c496f6ae0b6be2e0ce8d8eea2dda5c1e5b57c.jpg)

<details>
<summary>text_image</summary>

W₁^Q
W₁^K
W₁^V
</details>

![](images/a92a9447a35ee4bd35b53f1ff5fb2347c92627230efbd54eb2980713a474509a.jpg)

![](images/2332d2485d8ac7edd2a7a1f621fb78085b46030f700657ca98bbb6a07f30f4e5.jpg)

![](images/f67ff6af5cc34b0db17e2cebb081a47678048e3929ed94378c066ea936fbf28b.jpg)

<details>
<summary>text_image</summary>

W7^Q
W7^K
W7^V
</details>

![](images/c57507cc5e625095aafd206153f6f35175642144784cfa53244f37a915c5df27.jpg)

<details>
<summary>text_image</summary>

Q₇
K₇
V₇
</details>

![](images/02e3b6be5ebc7b381012995c0790f890e85a6ebe336c6b2bf910785ee3c870b4.jpg)

![](images/3020caf34ab73ded901936de3e9ad182405bd84045271e58b80c49f53318eb9b.jpg)

<details>
<summary>text_image</summary>

W⁰
Z
</details>

## Cross-Attention (e.g. Machine Translation)

![](images/416ccd23c3213c8b150a49e129eb9bc34e76c03718589ff6f8e9df8d43549780.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Start"] --> B["Student"]
  B --> C["Intermediate Layer"]
  C --> D["Student"]
  D --> E["Intermediate Layer"]
  E --> F["Student"]
  F --> G["Intermediate Layer"]
  G --> H["Student"]
  H --> I["Intermediate Layer"]
  I --> J["Student"]
  J --> K["Intermediate Layer"]
  K --> L["Student"]
  L --> M["Intermediate Layer"]
  M --> N["Student"]
  N --> O["Intermediate Layer"]
  O --> P["Student"]
  P --> Q["Intermediate Layer"]
  Q --> R["Student"]
  R --> S["Intermediate Layer"]
  S --> T["Student"]
  T --> U["Intermediate Layer"]
  U --> V["Student"]
  V --> W["Intermediate Layer"]
  W --> X["Student"]
  X --> Y["Intermediate Layer"]
  Y --> Z["Student"]
  Z --> AA["Intermediate Layer"]
  AA --> AB["Student"]
  AB --> AC["Intermediate Layer"]
  AC --> AD["Student"]
  AD --> AE["Intermediate Layer"]
  AE --> AF["Student"]
  AF --> AG["Intermediate Layer"]
  AG --> AH["Student"]
  AH --> AI["Intermediate Layer"]
  AI --> AJ["Student"]
  AJ --> AK["Intermediate Layer"]
  AK --> AL["Student"]
  AL --> AM["Intermediate Layer"]
  AM --> AN["Student"]
  AN --> AO["Intermediate Layer"]
  AO --> AP["Student"]
  AP --> AQ["Intermediate Layer"]
  AQ --> AR["Student"]
```
</details>

## Transformers vs. RNNs

<table><tr><td>Challenges with RNNs</td><td>Transformers</td></tr><tr><td>Long range dependenciesGradient vanishing and explosionLarge # of training stepsSequential/recurrence → can’t parallelizeComplexity per layer: O(n*d2)</td><td>Can model long-rangedependenciesNo gradient vanishing and explosionFewer training stepsCan parallelize computation!Complexity per layer: O(n2*d)</td></tr></table>

## Large Language Models

▶ Scaled up versions of Transformer architecture, e.g. millions/billions of parameters  
▶ Typically trained on massive amounts of “general” textual data (e.g. web corpus)  
▶ Training objective is typically “next token prediction”: $P(W_{t+1}|W_t, W_{t-1}, \ldots, W_1)$  
Emergent abilities as they scale up (e.g. chain-of-thought reasoning)  
▶ Heavy computational cost (time, money, GPUs)  
▶ Larger general ones: “plug-and-play” with few or zero-shot learning  
▶ Train once, then adapt to other tasks without needing to retrain  
▶ E.g. in-context learning and prompting

## Emergent Abilities of Large Language Models

▶ Why do LLMs work so well? What happens as you scale up?  
▶ Potential explanation: emergent abilities!  
▶ An ability is emergent if it is present in larger but not smaller models  
▶ Not have been directly predicted by extrapolating from smaller models  
▶ Performance is near-random until a certain critical threshold, then improves heavily  
▶ Known as a “phase transition” and would not have been extrapolated

## Few-Shot Prompting

![](images/2cd63f664185e237a40fdb1ced5e58be8f92bde0987d11d923ccf8d1755784ee.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["Input"] --> B["Review: This movie sucks.<br>Sentiment: negative.<br>Review: I love this movie.<br>Sentiment:"]
  B --> C["Language model"]
  C --> D["Output<br>positive."]
```
</details>

Figure 1: Example of an input and output for few-shot prompting.

![](images/cebfd21ac3a13beb9b9e14d98b4b6421ccdcf72cb9e2a101cafb659cdcb58f3c.jpg)

![](images/642d897269a93d8fe7e475f23bbc5a169ac6251418d22eff6360c219576ee4ad.jpg)

<details>
<summary>line chart</summary>

| x        | Accuracy (%) |
| -------- | ------------ |
| 10^18    | 0            |
| 10^20    | 0            |
| 10^22    | 0            |
| 10^24    | 35           |
</details>

![](images/b9c30f19dc567b8a6d74a431d4238c11f2218e3e95904c80280a414b16e621e4.jpg)

<details>
<summary>line chart</summary>

| X (log scale) | BLEU (%) |
| ------------- | -------- |
| 10^18         | 0        |
| 10^20         | 0        |
| 10^22         | 0        |
| 10^24         | 25       |
</details>

![](images/d4ff6a2cf1b30ccf558b4e8fcd8813e57537dcf32790fd79fc07c6777ef9f52b.jpg)

<details>
<summary>line chart</summary>

| x        | Exact match (%) |
| -------- | --------------- |
| 10^18    | 0               |
| 10^20    | 0               |
| 10^22    | 0               |
| 10^24    | 15              |
</details>

![](images/3338973e4a4c9920b308c856effed53f3cfb9422c4c822fd06d924247b12c594.jpg)

<details>
<summary>line chart</summary>

| x        | Exact match (%) |
| -------- | --------------- |
| 10^18    | 25              |
| 10^20    | 24              |
| 10^22    | 26              |
| 10^24    | 45              |
</details>

![](images/7c9d83e147541f81851e0fde0f8baf692000f724f010f05440d7488362850686.jpg)

<details>
<summary>line chart</summary>

| x      | Accuracy (%) |
| ------ | ------------ |
| 10^20  | 20           |
| 10^22  | 20           |
| 10^24  | 45           |
</details>

![](images/f2770757895306997d0eff5a8dd3edd97c109287dec220ec19182aa0d249be46.jpg)

<details>
<summary>line chart</summary>

| x        | Accuracy (%) |
| -------- | ------------ |
| 10^20    | 10           |
| 10^21    | 8            |
| 10^22    | 15           |
| 10^23    | 45           |
</details>

![](images/bc83a664fe7be24d66d49cc0c68c8e0dff1887fe263afa87bdbb4f10d2413968.jpg)

<details>
<summary>line chart</summary>

| x      | Accuracy (%) |
| ------ | ------------ |
| 10^20  | 25           |
| 10^22  | 30           |
| 10^24  | 60           |
</details>

![](images/00633b276b894273dce02863bbabe9b625d6f56399ab6f2e92f7267fa7a51a0f.jpg)

<details>
<summary>line chart</summary>

| Word in context | Accuracy (%) |
| --------------- | ------------ |
| 10^20           | 50           |
| 10^22           | 48           |
| 10^24           | 65           |
</details>

Model scale (training FLOPs)  
Figure 2: Eight examples of emergence in the few-shot prompting setting. Each point is a separate model. The ability to perform a task via few-shot prompting is emergent when a language model achieves random performance until a certain scale, after which performance significantly increases to well-above random. Note that models that used more training compute also typically have more parameters—hence, we show an analogous figure with number of model parameters instead of training FLOPs as the x-axis in Figure 11. A–D: BIG-Bench (2022), 2-shot. E: Lin et al. (2021) and Rae et al. (2021). F: Patel & Pavlick (2022). G: Hendrycks et al. (2021a), Rae et al. (2021), and Hoffmann et al. (2022). H: Brown et al. (2020), Hoffmann et al. (2022), and Chowdhery et al. (2022) on the WiC benchmark (Pilehvar & Camacho-Collados, 2019).

## Potential Explanations of Emergence

▶ Currently few explanations for why these abilities emerge  
▶ Evaluation metrics used to measure these abilities may not fully explain why they emerge  
▶ Disclaimer: maybe emergent abilities of LLMs are a mirage!!!

https://arxiv.org/abs/2304.15004  
▶ “Emergent abilities appear due to the researcher's choice of metric rather than due to fundamental changes in model behavior with scale”

## Beyond Scaling

▶ Further scaling could endow even-larger LMs with new emergent abilities  
▶ While scaling is a factor in emergent abilities, it is not the only factor  
▶ E.g. new architectures, higher-quality data, and improved training procedures, could enable emergent abilities on smaller models  
▶ Further research may make the abilities available for smaller models  
▶ Other directions: improving few-shot prompting abilities of LMs, theoretical and interpretability research, and computational linguistics work

## Questions for the Group

▶ Do you believe emergent abilities will continue to arise with more scale? Will there be a limit? Possibly even diminishing returns?  
What are your thoughts on the current trend of larger models and more data? Do you believe this is a good direction for the research community, or rather “inhibiting our creativity”?  
Thoughts on retrieval-based or retrieval-augmented systems compared to simply “learning everything” within the parameters of the model?

## RLHF, ChatGPT, GPT-4, Gemini

## Reinforcement Learning with Human Feedback (RLHF)

▶ RLHF: technique that trains a "reward model" directly from human feedback  
▶ Uses the model as a reward function to optimize an agent's policy using reinforcement learning (RL) through an optimization algorithm  
▶ Ask humans to rank instances of the agent's behavior, e.g. which produced response is better

![](images/8aef13084a6f04900b799c57b8b7f99f0f8f566f16efce434fe5b5333ad4878a.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["RL ALGORITHM"] -->|PREDICTED REWARD| B["REWARD PREDICTOR"]
  B -->|OBSERVATION| C["ENVIRONMENT"]
  C -->|ACTION| A
    B -.->|HUMAN FEEDBACK| C
```
</details>

## Direct Preference Optimization (DPO)

![](images/ed6d948129a9963308591bdbe8c406d589576b68e7974a857922b23dd827981e.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["preference data"] --> B["maximum likelihood"]
  B --> C["reward model"]
  C --> D["label rewards"]
  D --> E["sample completions"]
  E --> F["LM policy"]
    style A fill:#f9f,stroke:#333
    style F fill:#bbf,stroke:#333
    subgraph Reinforcement_learning_from_Human_Feedback_RLHF["\"Reinforcement learning from Human Feedback (RLHF)\""]
        G["write me a poem about the history of jazz"]
    end
```
</details>

![](images/87ea63c55e7e8c15dc104a5fff203f25fe82d30d181f230a1583ec44cb4906c7.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["preference data"] --> B["> y_w"]
  B --> C["maximum likelihood"]
  C --> D["final LM"]
```
</details>

Figure 1: DPO optimizes for human preferences while avoiding reinforcement learning. Existing methods for fine-tuning language models with human feedback first fit a reward model to a dataset of prompts and human preferences over pairs of responses, and then use RL to find a policy that maximizes the learned reward. In contrast, DPO directly optimizes for the policy best satisfying the preferences with a simple classification objective, without an explicit reward function or RL.

## ChatGPT

▶ Finetuned on GPT-3.5, which is a series of models trained on a mix of text and code using instruction tuning and RLHF  
▶ Taken the world by storm!

![](images/7b5cb2dee9bb76112ffd10acf700b1e44eebed8d11ecfb2b4efae746861020a3.jpg)

<details>
<summary>text_image</summary>

Welcome to ChatGPT
in your OpenAI accoun!
Hello! How can I help you today? Is
there something you need help with or
would like to learn more about? I'm
here to assist you with any questions
you may have.
</details>

![](images/14908bc35cc0a9709b02f6562a9f6950f0e98a3b2063f3560dabbc9554c80bf5.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["GPT-3 Series"] --> B["Training on code"]
  B --> C["Codex Initial"]
  C --> D["Code-davinci-001"]
  C --> E["Code-cushman-001"]
  F["Large-scale language model pretraining"] --> G["GPT-3 Initial"]
  G --> H["Davinci"]
  H --> I["InstructGPT Initial"]
  I --> J["Instruct-davinci-beta"]
  I --> K["Text-davinci-001"]
  L["GPT-3.5 Series"] --> M["Code-davinci-002"]
  M --> N["Supervised instruction tuning"]
  N --> O["RLHF"]
  O --> P["Text-davinci-002"]
  P --> Q["ChatGPT"]
  R["LM + code training then instruction tuning"] --> M
```
</details>

## GPT-4

▶ Supervised learning on large dataset, then RLHF and RLAIF  
▶ GPT-4 trained on both images and text, vision is also out!  
▶ Discuss humor in images, summarize screenshot text, etc.  
▶ GPT-4 is "more reliable, creative, and able to handle much more nuanced instructions than GPT-3.5"  
▶ Much longer context windows of 8,192 and 32,768 tokens  
▶ Does exceptionally well on standardized tests  
▶ Did not release technical details of GPT-4

![](images/e40945f4ad896d3939f64d4556ee0c2390903423c1b2645d7f0304164ad38596.jpg)

<details>
<summary>natural_image</summary>

Close-up of a humanoid robot head with exposed mechanical components and a circular dial (no text or symbols visible)
</details>

GPT-4

## Gemini

▶ Latest: Gemini 1.5 Pro  
Gemini Ultra performs better than ChatGPT on 30 of the 32 academic benchmarks in reasoning and understanding it tested on.  
▶ Effectively processes and integrates data from diff modalities:  
▶ Text, audio, image, video  
▶ Based on a Mixture-of-Experts (MoE) model  
▶ Significantly improves efficiency in training and application

## Gemini

▶ Based on a Mixture-of-Experts (MoE) model

Combination of multiple small Neural networks known as ‘Experts’ which are trained and capable of handling particular data and performing specialised tasks.  
▶ 'Gating network' which predicts which response is best suited to address the request.

![](images/ddd8bbfa1879c2a226ec66fca4f2aafa85b38bfed13973da5b73043ced5719eb.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Input"] --> B["Text Expert"]
  A --> C["Image Expert"]
  A --> D["Fusion Expert"]
  B --> E["Output"]
  C --> E
  D --> E
  E --> F["Gating Network"]
  F --> A
    style A fill:#f9f,stroke:#333
    style E fill:#bbf,stroke:#333
    style F fill:#dfd,stroke:#333
```
</details>

Source link

## Where we are (2024)

## Recently Taken Off:

- LLM boom: ChatGPT, GPT-4, Gemini, open-source models  
• Human alignment and interaction

\- Reinforcement learning & human feedback

• Controlling toxicity, bias, and ethics  
- More use in unique applications: audio, art/music, neuro/bio, coding, games, physical tasks, etc.

\- Speakers will touch (or have touched) on these!

\- Other: diffusion models (e.g. text-to-image/video gen)

○ Also, Diffusion Transformer (DiT)

![](images/3c119313680ad1df8bb89f9902c8fef00d8ae9216fc32991c3edc7eec5fcdc25.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Examples"] --> B["&quot;Explain quantum computing in simple terms&quot; →"]
  C["Capabilities"] --> D["Remembers what user said earlier in the conversation"]
  E["Limitations"] --> F["May occasionally generate incorrect information"]
  G["&quot;Got any creative ideas for a 10 year old's birthday?&quot; →"] --> H["Allows user to provide follow-up corrections"]
  I["&quot;How do I make an HTTP request in Javascript?&quot; →"] --> J["Trained to decline inappropriate requests"]
  K["Limited knowledge of world and events after 2021"] --> L["May occasionally produce harmful instructions or biased content"]
```
</details>

Image source: https://openai.com/blog/chatgpt/

![](images/046287344118d6bf35d567dc476ba3d1d612f0beec3b4f4f54bbd559f817bc39.jpg)

<details>
<summary>text_image</summary>

TEXT PROMPT an armchair in the shape of an avocado, an armchair imitating an avocado.
AI-GENERATED IMAGES
</details>

## The Future (What's Next?)

\- Can enable a lot more applications:

○ Generalist Agents  
- Longer video understanding and generation, finance + business  
- Incredibly long sequence modeling (GPT authors a novel)  
○ Domain-specific “Foundation models” - DoctorGPT, LawyerGPT, ...  
- Potential real-world impacts:

■ Personalized education and tutoring systems  
■ Advanced healthcare diagnostics, environmental monitoring & protection, etc.  
■ Real-time multilingual communication  
■ Interactive entertainment & gaming (e.g. NPCs)

![](images/f2e9cd4a604da2d2c34f40604e426192a2667088cde3c37aedce958a8a37018e.jpg)

<details>
<summary>natural_image</summary>

Futuristic futuristic spaceship with reflective surface and dynamic design (no visible text or symbols)
</details>

## The Future (What's Missing?)

## • Missing Ingredients (to AGI/ASI?):

○ Reducing computation complexity  
- Enhanced human controllability  
○ Alignment with language models of human brain  
○ Adaptive learning and generalization across domains  
○ Multi-sensory multimodal embodiment (e.g. intuitive physics and commonsense)  
- Infinite/external memory: like Neural Turing Machines  
- Infinite/constant self-improvement and self-reflection capabilities  
○ Complete autonomy and long-horizon decision-making  
○ Emotional intelligence and social understanding  
- Ethical reasoning and value alignment

![](images/dad53402338ab215df69d1d0e62fac8261f576d63e16041cebc8248ca7a1a8d4.jpg)

<details>
<summary>natural_image</summary>

Futuristic futuristic spaceship with reflective surface and dynamic control panels (no text or symbols visible)
</details>

## Major Applications of Transformers

## Text and Language

![](images/12304c35e8c6d62bde5f9f99d8d8edfaab881562f1ff6753caab4f7a933187fb.jpg)

<details>
<summary>text_image</summary>

ChatGPT
What are you?
I'm a large language model trained by OpenAI. I'm a form of artificial intelligence that has been designed to process and generate human-like language.
Are you human?
I'm not a human and I don't have the ability to think or feel in the same way that a person does.
</details>

## Audio: Speech + Music

![](images/7347bb509c4584459d7614a4123533f1f17f3d32089ca4180f5fd859fbadb27b.jpg)

<details>
<summary>text_image</summary>

OpenAI/whisper
</details>

## Vision: Analyzing Images & Videos

![](images/24938a3b5bcb09fd466fc9aff9f42ad90c61c017c3f6998094260c374daab335.jpg)

<details>
<summary>text_image</summary>

OBJECT
OBJECT
OBJECT
OBJECT
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
PERSON
Person
</details>

Vision Transformer (ViT)

## Vision: Generating Images & Video

![](images/226d0d28d0e936fae1166d16b9de8bbf2945b05210628c1e8a241c35a88405fc.jpg)

<details>
<summary>natural_image</summary>

Collage of diverse images including animals, landscapes, and urban scenes with no visible text or symbols.
</details>

Images: Stable Diffusion, Dall-E, Midjourney, etc.  
Videos: Sora, Pika, etc.

## Robotics, Simulations, Physical Tasks

![](images/71a2c5ec62b9f885c773b85bbc5099e0c3d492eefaf10dd3d7ef44d0023075d3.jpg)

<details>
<summary>text_image</summary>

Mine Amethyst
</details>

![](images/5697c0d696cc715ffa4a37f75a540fa2c427f7a4a88b1bf2dc1dc3aa165ead99.jpg)

<details>
<summary>natural_image</summary>

3D-rendered geometric structure resembling a tunnel or maze with scattered green and blue elements, labeled 'Gather Cactus' at bottom (no other text or symbols)
</details>

![](images/8d2c15cd64e2dedfbe6706aa09bd02dc4d6cee27a837878d7311718bb02cc22a.jpg)

<details>
<summary>text_image</summary>

Build Base
</details>

![](images/1994388bd39c9453ba8f2372bea79e8c44aaf33f0fef4433cc91a78d98499406.jpg)

<details>
<summary>natural_image</summary>

Pixelated black-and-white scene of a rocky wall with green vegetation and a small character, labeled 'Fight Enderman' in the corner (no other text or symbols)
</details>

![](images/f137afc332eade5774d47e6736bda615684a6eb0c5b0953039459c684da77264.jpg)

<details>
<summary>natural_image</summary>

3D-rendered game scene with a person climbing a wooden block on grass, labeled 'Build House with Human Feedback' (no other text or symbols)
</details>

![](images/050fd94bd9af0c6c12ff2ce962cf5991fcf9ee73e3fa7c7d6721840317637771.jpg)

<details>
<summary>natural_image</summary>

Minecraft-style stone tunnel interior with pixelated texture and a small inset image labeled 'Mine Gold' (no other text or symbols)
</details>

![](images/a75f8749ec8971af7b553120bafda52f32799e09a8d1523b22265b8038e977af.jpg)

<details>
<summary>natural_image</summary>

Pixel art scene showing a person standing in front of stone and brick structures (no text or symbols visible)
</details>

![](images/c784e4fa73f2c247326244e6a6d7463abaad2f6e45b8c8ce7538ba5380d7f2bb.jpg)

<details>
<summary>natural_image</summary>

Minecraft-style scene showing a red brick structure surrounded by greenery and trees, with no visible text or symbols.
</details>

![](images/b55ef9a0a936e6c8f2fe0de6d7d95018e5130fa2325ebc854fe8cac4c83c3fc7.jpg)

<details>
<summary>natural_image</summary>

3D-rendered game scene with a tank, blue water blocks, and a small figure in the sky (no text or symbols)
</details>

Build Nether Portal with Human Feedback

E.g. Voyager, Mobile ALOHA

![](images/3825e40c4279df263a7f8c78de98ef034d60648ff103fb0f0c949869db23023d.jpg)

<details>
<summary>natural_image</summary>

Person using a robotic balance system in a kitchen setting (no visible text or symbols)
</details>

Learned Policies  
![](images/a46491b484a7c4ead4d098ec860ff976cf8eb657ff961a4150c799bfd867e844.jpg)

<details>
<summary>natural_image</summary>

Robot arm stirring a pan on an induction cooktop (no visible text or symbols)
</details>

cook shrimp

![](images/7292b6a233d6905373ba3ce61d4ef1b745a244c5f0f2eb154a8f23fdce24ebfb.jpg)

<details>
<summary>natural_image</summary>

Interior view of a modern office or lab space with a robotic arm and workbench (no visible text or symbols)
</details>

push chairs

![](images/66851c91727477a662707faf6fbe5142902031ff399ffc31ad271c417c81422e.jpg)

<details>
<summary>natural_image</summary>

Interior view of a laboratory or lab setup with robotic arm and computer monitor (no visible text or symbols)
</details>

use cabinet

![](images/ae2ea3e8531d7abe69fba46e53d9ac5eb40fbed0a15d61814247a00950b0730c.jpg)

<details>
<summary>natural_image</summary>

Person operating a robotic arm in a lab setting (no visible text or symbols)
</details>

wipe wine

![](images/b34e04d3803dc7fbd8df8f918ee16b6c6fd6da4eb2246b99108c89d70155bbaf.jpg)

<details>
<summary>natural_image</summary>

Laboratory setup with mechanical equipment and a display panel (no visible text or symbols)
</details>

call elevator

![](images/07b1dcb2821a212ee15141c97a315602f269f998f7affea3c628adde9db6f4bb.jpg)

<details>
<summary>natural_image</summary>

Interior view of a modern office with two people interacting near a robotic arm and a monitor (no visible text or symbols)
</details>

high five

## Playing Games

![](images/c756a65e18fda644f6227b292fb8cfa9a80e1b2e59abee58b981c7c3504c3993.jpg)

<details>
<summary>text_image</summary>

ALPHAGO
THE BISCH ON THE FESTIVAL 2017
BELONDON FILM FESTIVAL OFFICIAL SELECTION 2017
WINNER
INTERNATIONAL COUNCING ITEMS
OFFICIAL SELECTION 2017
MILL VALLEY FILM FESTIVAL
Official Selection WARRIARY FILM FESTIVAL
VIFS
</details>

E.g. AlphaGo, AlphaStar,  
AI for MOBAs (e.g. Dota 2 / LoL)

![](images/2bc4ab49b6f315f67bd466db243bdb5bfa66948a960166d149ee814ed4a5f54c.jpg)

<details>
<summary>text_image</summary>

DeepMind
</details>

![](images/b53382f6390019711da745842d0ca4d764609a7fcc803b347c626ef3342d4f6c.jpg)

<details>
<summary>text_image</summary>

D/A 3/0/2
DN 24/9
OPENAI FIVE
OpenAI 5 (Bat)
DEATH PROPIET
12+2.4
4.8
359.2
31+1
20+1
33+7
463 794 +2.4
190 675 +2.7
OpenAI
309
</details>

## Biology + Healthcare

![](images/419ef47b6e02bf985ae295782055b73c174959a817b7d1357a6c6ba0465d7504.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Med-PaLM M"] --> B["Mammography"]
  A --> C["Genomics"]
  A --> D["Radiograph"]
  A --> E["Radiology Report"]
  A --> F["Medical Knowledge"]
  A --> G["Pathology"]
  A --> H["Dermatology"]
  A --> I["Medical Question Answering"]
  A --> J["Medical Visual Question Answering"]
  A --> K["Medical Image Classification"]
  A --> L["Radiology Report Summarization"]
  A --> M["Radiology Report Generation"]
  A --> N["Genomic Variant Calling"]
    style A fill:#f9f9f9,stroke:#333
    style B fill:#e6f7ff,stroke:#333
    style C fill:#e6f7ff,stroke:#333
    style D fill:#e6f7ff,stroke:#333
    style E fill:#e6f7ff,stroke:#333
    style F fill:#e6f7ff,stroke:#333
    style G fill:#e6f7ff,stroke:#333
    style H fill:#e6f7ff,stroke:#333
    style I fill:#e6f7ff,stroke:#333
    style J fill:#e6f7ff,stroke:#333
    style K fill:#e6f7ff,stroke:#333
    style L fill:#e6f7ff,stroke:#333
    style M fill:#e6f7ff,stroke:#333
    style N fill:#e6f7ff,stroke:#333
```
</details>

E.g. Med-PaLM, AlphaFold

![](images/6eae70e5f784bf60b46ce5c2ccc34c201e05f3c4a40b511cf7c6fb5312828832.jpg)

<details>
<summary>text_image</summary>

Google
INTRODUCING
MED-PALM 2
</details>

![](images/ac09e8a2e46710a1a092d7c89e1c767f674864dbba599eb24609095b23f49ea5.jpg)

<details>
<summary>natural_image</summary>

3D molecular structure visualization with Google DeepMind logo and ribbon representation (no text or symbols on the diagram itself)
</details>

## Recent Trends and Remaining Weaknesses of LLMs

## Requiring Large Amounts of Data, Compute, and Cost

▶ Current LLMs take immense amounts of data, compute, and \$ to train  
▶ Requires training over weeks/months over thousands of GPUs  
▶ BabyLM challenge: can we train LLMs using similar amounts of data a baby is exposed to while growing up?

## BabyLM: Children vs. LLMs

▶ Children are different due to several reasons:

▶ LMs do statistical learning, which requires more data to learn statistical relations between words and get abstraction/generalization/reasoning  
▶ Children may learn in smarter, e.g. more explicit compositional/hierarchical manners, learning abstraction/generalization/reasoning more easily

## BabyLM: Children vs. LLMs

Thoughts/ideas from Michael C. Frank's tweet  
▶ 4-5 orders of input magnitude diff b/w human and LLM emergence  
▶ Factor 1: Innate knowledge - relates to priors  
▶ Factor 2: multimodal grounding  
▶ Factor 3: active, social learning  
▶ Factor 4: evaluation differences

![](images/a28aa926c5bad57d97211800b7db12a21399b876f87de1588d42296441752b5a.jpg)

<details>
<summary>line chart</summary>

| Age (years) | human upper bound | human lower bound |
| ----------- | ----------------- | ----------------- |
| 0           | 7.0               | 6.0               |
| 5           | 7.2               | 6.2               |
| 10          | 7.5               | 6.5               |
| 20          | 8.0               | 7.0               |
</details>

## Minified LLMs and On-Device LLMs

▶ Big trend of using LLMs for applications and everyday purposes  
▶ A requirement is ability to run quickly and easily on-devices  
▶ AutoGPT and ChatGPT “plug-ins”  
▶ Right now, work on smaller open-source models (e.g. LLaMA, Mistral)  
In the future: ability to finetune and run models locally, even on your phone!  
▶ Getting more possible due to more open-source, but still very large and \$

## Memory Augmentation & Personalization

▶ Weakness of LLMs is that they are frozen in knowledge at a particular point in time, and don’t augment knowledge “on the fly”  
▶ Hope to be able to remember the information while chatting with a particular user, both within the same conversation and across conversations  
▶ Would help with context window limits and adapting to the particular user  
Widescale: somehow update the model “on the fly” with info from several users  
▶ Further, they usually do not adapt their talking style and persona to the particular user, which could have applications such as mental health therapy

## Memory Augmentation & Personalization

▶ Potential approaches:

▶ Memory bank - not feasible/efficient with larger amounts of data  
▶ Prefix-tuning approaches (finetune a small part of the model) - too expensive  
▶ Some prompt-based approach - do not see how this would be possible to change the model itself, but can at least help it “personalize” to the user  
▶ RAG: retrieval-augmented generation (data store, augment context each time)

▶ Relies on high-quality external data store

▶ Typically not end-to-end

▶ Not within the “brain” of the model but outside:

▶ Suitable for knowledge/facts, but not fundamental capabilities and skills

## Pretraining Data Synthesis & Selection

▶ Lots of work these days on synthetic data generation (e.g. using GPT-4) to train other models, e.g. smaller models or peer models  
▶ More work on understanding how to best synthesize and select the pretraining data  
▶ Related to model distillation: knowledge from a large complex model (Teacher) is transferred to a smaller, more efficient model (student)  
▶ Goal: achieve similar performance with less computational cost  
▶ Example: Microsoft Phi models (“Textbooks Are All You Need!”)  
https://arxiv.org/abs/2306.11644  
Nathan Lambert's Summary on Synthetic Data in his Interconnect Newsletter

## Microsoft Phi-2 Model

▶ Phi-2, a 2.7 billion-parameter model, excels in reasoning and language understanding, challenging models up to 25x larger  
▶ Emphasizes "textbook-quality" training data and synthetic datasets for teaching common sense and general knowledge  
▶ Training data mix: synthetic datasets to teach the model commonsense reasoning and general knowledge, including science, daily activities, ToM, etc.  
▶ Further carefully selected web data filtered by educational value + content quality  
▶ Phi-2 designed as a resource for research on interpretability, safety improvements, and fine-tuning across tasks

## New Knowledge or “Memorizing”?

When LLM is prompted and says something, is what it says truly “novel/new”?  
▶ Innovation vs. Regurgitation: ongoing debates about whether LLMs can truly invent new ideas or are primarily recombining existing knowledge (since learn patterns from lots of text)  
▶ Test-time Contamination: models might regurgitate rather than synthesize information due to overlap between training and evaluation data, leading to misleading benchmark results  
▶ Cognitive Simulation: some argue LLMs mimic human thought processes, suggesting a form of "understanding," while others see this as simply "sophisticated pattern matching"  
▶ Ethical and Practical Implications: this impacts trustworthiness, copyright issues, and the educational use of LLM outputs  
▶ E.g. copyright lawsuit by New York Times (NYT) on OpenAI!

## Continual Learning

▶ AKA, infinite and permanent fundamental self-improvement  
▶ Similar to humans: we constantly learn everyday from every interaction  
- Don’t need to “finetune ourselves” once in a while

▶ Very challenging, could be the key to AGI!

Currently work on: finetune a small model based on traces from better model or same model after filtering those traces

▶ More like re-training and distillation than true “continual learning”

▶ Work showing that reasonably sampled data with interjected augmented reasoning and further filtering can be used to further finetune or optimize (e.g. using DPO)

▶ E.g. UltraChat-200k and Zephyr

▶ E.g. LLMs Can Self-Improve paper

## Interpretability of LLMs

▶ Enormous number of parameters trained on tons of data → “huge black-box” that is hard to interpret and understand  
▶ More work on interpretability is required  
▶ Would allow us to better understand models, leading to better ideas of what/how to improve, easier control, and better alignment/safety  
▶ Mechanistic interpretability: understand how individual components + operations in an ML model contribute to its overall decision-making process  
▶ Goal: unpack the "black box" of models for clearer insight into how they work

## Model Editing & Mechanistic Interpretability

▶ Also work on mechanistic interpretability and model editing (e.g. edit specific nodes)  
▶ Relevant paper: https://arxiv.org/abs/2202.05262  
▶ Development of a causal intervention method to trace decisive neuron activations for model factual predictions

Rank-One Model Editing (ROME) to modify model weights for updating factual associations

▶ Mid-layer feedforward modules play a significant role in storing factual associations

▶ Manipulation of these can be a feasible approach for model editing

![](images/a52227e23e9e7253b118ee4fdac78415b1e18ab996dd6fdb0b981b8899d1a7dc.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
    subgraph Clean run
  A["The"] --> B["Space"]
  C["Need"] --> D["le"]
  E["is"] --> F["in"]
  G["downtown"] --> H["..."]
    end

    subgraph Patch clean states
  I["Seattle downtown (correct output)"] --> J["Corrupted subject run"]
  K["The*"] --> L["Space*"]
  M["Need*"] --> N["le*"]
  O["is"] --> P["in"]
  Q["..."] --> R["Corrupted embedding example flow"]
    end

    subgraph Output state
  S["Corrupted output ?"] --> T["Note when output is fixed"]
    end

    style Clean run fill:#f9f,stroke:#333
    style Patch clean states fill:#ccf,stroke:#333
    style Output state fill:#cfc,stroke:#333
```
</details>

(e) Impact of restoring state after corrupted input  
![](images/8538b796059f72bcca424f8ee81b5c736f22275fa5e50adcec0406b22fa9b271.jpg)

<details>
<summary>heatmap</summary>

| | The* | Space* | Need* | le* | is | in | downtown |
|---|---|---|---|---|---|---|---|
| p(Seattle) | 0.8 | 0.6 | 0.4 | 0.2 | 0.1 | 0.05 | 0.02 |
| (various values) | 0.8 | 0.6 | 0.4 | 0.2 | 0.1 | 0.05 | 0.02 |
</details>

(f) Impact of restoring MLP after corrupted input  
![](images/193bdb140af2b879ff3872a1555d48f5ca1387bfcbe11b4a75754ac1dc87d0d4.jpg)

<details>
<summary>heatmap</summary>

| | center of interval of 10 restored MLP layers | p(Seattle) |
|---|---|---|
| The* | 10 | 0.8 |
| Space* | 15 | 0.6 |
| Need* | 20 | 0.4 |
| le* | 25 | 0.2 |
| is | 30 | 0.1 |
| in | 35 | 0.05 |
| downtown | 40 | 0.02 |
</details>

(g) Impact of restoring Attn after corrupted input  
![](images/a9be5a93a2065d9b7394a948ee23a4d33f669a6ba30f7b215ddf8a708f02277e.jpg)

<details>
<summary>heatmap</summary>

| center of interval of 10 restored Attn layers | p(Seattle) |
|---|---|
| The* | 0.6 |
| Space* | 0.4 |
| Need* | 0.2 |
| le* | 0.3 |
| is | 0.5 |
| in | 0.6 |
| downtown | 0.7 |
</details>

Figure 1: Causal Traces compute the causal effect of neuron activations by running the network twice: (a) once normally, and (b) once where we corrupt the subject token and then (c) restore selected internal activations to their clean value. (d) Some sets of activations cause the output to return to the original prediction; the light blue path shows an example of information flow. The causal impact on output probability is mapped for the effect of (e) each hidden state on the prediction, (f) only MLP activations, and (g) only attention activations.

## Model Modularity + Mixture of Experts (MoE)

▶ Mixture of Experts (MoE) very prevalent these days in LLMs:

▶ E.g. GPT-4, Gemini, etc.

▶ Goal: have several models/“experts” work together to solve a problem

▶ Each expert may be specialized for a task/purpose

▶ Try to use the diff skill-sets together to arrive at a generation

▶ Research on how to better define and connect these “experts”

![](images/143ecb4aa2e6dd118fe6af297920fa2606c9d558856faa01a6dbc1bb79683fe6.jpg)

<details>
<summary>flowchart</summary>

Neural network architecture diagram showing weights generated by gating and expert inputs, with weights flowing from input to output.
</details>

## Model Modularity + Mixture of Experts (MoE)

▶ Single model variation (?)

▶ Potential to segment/compartmentalize a single NN model into different compartments with their own focus, similar to the human brain?

▶ E.g. part of the network for fact-based info, another for spatial reasoning, another for mathematical + logical reasoning, etc.

▶ Maybe add more layers on top of the foundation model

▶ Particular layers correspond to something (e.g. new domain), and try to tune these new layers specifically

## Self-Improvement / Self-Reflection

▶ Found models can reflect on their own output to iteratively improve/refine them  
Examples of works: ReAct, Reflexion, Self-refine  
▶ Training LMs with Language Feedback: https://arxiv.org/abs/2204.14146

## Self-Improvement / Self-Reflection

▶ Tried multiple layers/levels of self-reflection... showing continual improvement

▶ Hypothesize that results will improve to a certain point and then degrade, and depends both on the model scale and the task at hand  
▶ Some folks believe that AGI is a “constant state of self-reflection”

▶ Can investigate further improvements to chain-of-thought reasoning and self-reflection

![](images/164829a52800af1bb8fbf0e125bb34a81d38583f33d0391f6b058cab8ea8d22b.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Query"] --> B["LLM"]
  B --> C["Action"]
  C --> D["Environment"]
  D --> E["Reward"]
  E --> F["{r₀,r₁,r₂,..., rₙ}"]
  F --> G["Heuristic (h)"]
  G --> H["Reflection (LLM)"]
  H --> A
  C --> I["{a₀,o₀,a₁,o₁,a₂,o₂,..., aₙ,oₙ}"]
  I --> G
```
</details>

Figure 1: Reflexion can be added to any decision-making approach. We enable ReAct agents to use self-reflection to improve their own performance.

![](images/cae99dce7cd8fb51e72e954530acfad4d8ff147a0e52fa5d0b609409d6a7eb51.jpg)

<details>
<summary>line chart</summary>

| Trials | Base  | Reflexion of Base | Reflexion of Reflexion of Base |
| ------ | ----- | ----------------- | ------------------------------ |
| 0      | 30.0  | 30.0              | 30.0                           |
| 2      | 35.0  | 60.0              | 70.0                           |
| 4      | 38.0  | 70.0              | 80.0                           |
| 6      | 39.0  | 75.0              | 85.0                           |
| 8      | 40.0  | 78.0              | 88.0                           |
| 10     | 40.0  | 80.0              | 90.0                           |
</details>

## Hallucination Problem

▶ Model “does not know when it does not know”  
▶ Due to sampling procedure, generates text and sounds confident about it, even when littered with factual errors  
▶ Can enhance through retrieval (e.g. through Google), and also:

▶ Internal-based “fact verification”  
▶ Output verification and regeneration (self-refinement/improvement)  
▶ Modifying the token sampling to shy away from hallucination  
▶ Some way to “predict hallucination” before generation and prevent it

▶ “Confidence” rating would also help check the reliability of the output

## Reasoning: Sufficient? Intermediate Guidance Helps...

▶ Chain-of-thought (CoT) - series of intermediate reasoning steps  
▶ Shown to improve LLM performance on complex reasoning tasks  
▶ Inspired by human thought process: decompose multi-step problems  
▶ Also provides an interpretable window into behavior of the model (how it arrived at an answer, where it goes wrong in its reasoning path)  
▶ CoT exploits the fact that deep down in the model's weights, it knows more about the problem than just prompting it to get a response

## Chain-of-Thought Reasoning

## Standard Prompting

## Model Input

Q: Roger has 5 tennis balls. He buys 2 more cans of tennis balls. Each can has 3 tennis balls. How many tennis balls does he have now?

A: The answer is 11.

Q: The cafeteria had 23 apples. If they used 20 to make lunch and bought 6 more, how many apples do they have?

## Model Output

A: The answer is 27.

![](images/ed9c5c934c404e3559a37026b9bbc08ea7fb168a2939e50bd6096aac24e48325.jpg)

## Chain-of-Thought Prompting

## Model Input

Q: Roger has 5 tennis balls. He buys 2 more cans of tennis balls. Each can has 3 tennis balls. How many tennis balls does he have now?

A: Roger started with 5 balls. 2 cans of 3 tennis balls each is 6 tennis balls. $5 + 6 = 11$ . The answer is 11.

Q: The cafeteria had 23 apples. If they used 20 to make lunch and bought 6 more, how many apples do they have?

## Model Output

A: The cafeteria had 23 apples originally. They used 20 to make lunch. So they had $23 - 20 = 3$ . They bought 6 more apples, so they have $3 + 6 = 9$ . The answer is 9.

## Improving Chain-of-Thought Reasoning

▶ CoT results in performance gains for larger LMs, but still remain a non-negligible fraction of errors  
▶ CoT error breakdown:

▶ 8% from just a calculator error  
▶ 16% from symbol mapping error  
▶ 22% from “one missing step” error

▶ Remaining errors due to semantic understanding issues and incoherent CoT

▶ We can investigate methods to address above errors and improve CoT in general

## Chain-of-Thought Reasoning for Smaller Models

Currently, CoT works effectively for models of approx. 100B params or more  
▶ Initial paper found “one-step missing” and “semantic understanding” CoT errors to be the most common among smaller models  
▶ 3 potential reasons:  
▶ Fail at even relatively easy symbol mapping tasks  
▶ Seem to have inherently weaker arithmetic abilities  
▶ Often had logical loopholes and did not arrive at a final answer  
▶ Improve CoT for smaller models → significant value to the research community

## Generalizing Chain-of-Thought Reasoning

▶ Find CoT to have a more rigid definition and format  
▶ Further, its advantages are for particular domains and types of questions  
▶ Task is challenging and requires multi-step reasoning  
▶ Scaling curve of the problem/task is relatively flat  
▶ Humans think through different types of problems in multiple ways  
▶ Our “scratchpad” is more flexible and open to different reasoning structures  
▶ Can maybe generalize CoT to be more flexible somehow

## Tree of Thoughts

▶ ToT: “consider multiple different reasoning paths and self-evaluating choices to decide the next course of action, as well as looking ahead or backtracking when necessary to make global choices”

![](images/0508f9773d0470760f6a1dfaf085e9d423f9aba2eddb1ab7241f3611fde1ab85.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Input"] --> B["Output"]
  C["Input"] --> D["Output"]
  E["Input"] --> F["Output"]
  G["Input"] --> H["Output"]
  I["Input"] --> J["Output"]
  K["Input"] --> L["Output"]
  M["Input"] --> N["Output"]
  O["Input"] --> P["Output"]
  Q["Input"] --> R["Output"]
  S["Input"] --> T["Output"]
  U["Input"] --> V["Output"]
  W["Input"] --> X["Output"]
  Y["Input"] --> Z["Output"]
  AA["Input"] --> AB["Output"]
  AC["Input"] --> AD["Output"]
  AE["Input"] --> AF["Output"]
  AG["Input"] --> AH["Output"]
  AI["Input"] --> AJ["Output"]
  AK["Input"] --> AL["Output"]
  AM["Input"] --> AN["Output"]
  AO["Input"] --> AP["Output"]
  AQ["Input"] --> AR["Output"]
  AS["Input"] --> AT["Output"]
  AU["Input"] --> AV["Output"]
  AW["Input"] --> AX["Output"]
    AY["Input-Output Prompting (IO)"]
    AZ["Chain of Thought Prompting (CoT)"]
    BA["Self Consistency with CoT (CoT-SC)"]
    BB["Majority vote"]
    BC["(d) Tree of Thoughts (ToT)"]
    BD["thought"]
```
</details>

Figure 1: Schematic illustrating various approaches to problem solving with LLMs. Each rectangle box represents a thought, which is a coherent language sequence that serves as an intermediate step toward problem solving. See concrete examples of how thoughts are generated, evaluated, and searched in Figures 246

## Socratic Questioning

▶ “Divide-and-conquer fashion algorithm that simulates the self-questioning and recursive thinking process.”  
“Self-questioning module using a large-scale LM to propose subproblems related to the original problem as intermediate steps and recursively backtracks and answers the sub-problems to reach the original problem.”

![](images/2572d354ec4e3e611c0745cac62cacb57ec395c7f4509949185d47f671cc916a.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Question: What fills the balloons?"] --> B["Floating in the air"]
  A --> C["Answer: Helium"]
  A --> D["Low density gas like helium"]
  B --> E["What is the state of the balloon?"]
  E --> F["Over people's heads"]
  F --> G["Where is the balloons?"]
  C --> H["What can make a balloon float?"]
  H --> I["Balloon has lower density"]
  I --> J["Why can a balloon float?"]
```
</details>

Figure 1: Example of a complex visual question solved in the human thinking process, involving raising visual and commonsense questions.

![](images/883197d29d7e047bd77876b8d08c4bbf71451d5ca2d52dcdf0efdb1ac301a30c.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Self-Questioning (SQ)"] --> B{H⁰} = [H⁰; H̃₁²]
  A --> C{H¹} = [H¹; H̃₁³]
  A --> D{H¹} = [H¹; H̃₂²]
  A --> E{H¹} = [H¹; H̃₁]
  A --> F{H¹} = [H², H̃₁]
  A --> G{H²} = [H³, H̃₁]
  A --> H{H³} = [H³; H̃₁]
  A --> I{H³} = [H³; H̃₁]
  A --> J{H³} = [H³; H̃₁]
  A --> K{H³} = [H³; H̃₁]
  A --> L{H³} = [H³; H̃₁]
  A --> M{H³} = [H³; H̃₁]
  A --> N{H³} = [H³; H̃₁]
  A --> O{H³} = [H³; H̃₁]
  A --> P{H³} = [H³; H̃₁]
  A --> Q{H³} = [H³; H̃₁]
  A --> R{H³} = [H³; H̃₁]
  A --> S{H³} = [H³; H̃₁]
  A --> T{H³} = [H³; H̃₁]
  A --> U{H³} = [H³; H̃₁]
  A --> V{H³} = [H³; H̃₁]
  A --> W{H³} = [H³; H̃₁]
  A --> X{H³} = [H³; H̃₁]
  A --> Y{H³} = [H³; H̃₁]
  A --> Z{H³} = [H³; H̃₁]
  A --> AA{H³} = [H³; H̃₁]
  A --> AB{H³} = [H³; H̃₁]
  A --> AC{H³} = [H³; H̃₁]
  A --> AD{H³} = [H³; H̃₁]
  A --> AE{H³} = [H³; H̃₁]
  A --> AF{H³} = [H³; H̃₁]
  A --> AG{H³} = [H³; H̃₁]
  A --> AH{H³} = [H³; H̃₁]
  A --> AI{H³} = [H³; H̃₁]
  A --> AJ{H³} = [H³; H̃₁]
  A --> AK{H³} = [H³; H̃₁]
  A --> AL{H³} = [H³; H̃₁]
  A --> AM{H³} = [H³; H̃₁]
  A --> AN{H³} = [H³; H̃₁]
  A --> AO{H³} = [H³; H̃₁]
  A --> AP{H³} = [H³; H̃₁]
  A --> AQ{H³} = [H³; H̃₁]
  A --> AR{H³} = [H³; H̃₁]
  A --> AS{H³} = [H³; H̃₁]
  A --> AT{H³} = [H³; H̃₁]
  A --> AU{H³} = [H³; H̃₁]
  A --> AV{H³} = [H³; H̃₁]
  A --> AW{H³} = [H³; H̃₁]
  A --> AX{H³} = [H³; H̃₁]
  A --> AY{H³} = [H³; H̃₁]
  A --> AZ{H³} = [H³; H̃₁]
  A --> BA{H³} = [H³; H̃₁]
  A --> BB{H³} = [H³; H̃₁]
  A --> BC{H³} = [H³; H̃₁]
  A --> BD{H³} = [H³; H̃₁]
  A --> BE{H³} = [H³; H̃₁]
  A --> BF{H³} = [H³; H̃₁]
  A --> BG{H³} = [H³; H̃₁]
  A --> BH{H³} = [H³; H̃₁]
  A --> BI{H³} = [H³; H̃₁]
  A --> BJ{H³} = [H³; H̃₁]
  A --> BK{H³} = [H³; H̃₁]
  A --> BL{H³} = [H³; H̃₁]
  A --> BM{H³} = [H³; H̃₁]
  A --> BN{H³} = [H³; H̃₁]
  A --> BO{H³} = [H³; H̃₁]
  A --> BP{H³} = [H³; H̃₁]
  A --> BQ{H⁰} = [H⁰; H̃₁²]
```
</details>

Figure 2: Overview of our SOCRATIC QUESTIONING framework.

## From Language Models to AI Agents

## Introduction

- Actions and Emergent Agent Architectures  
• Building Human-like AI Agents  
• Computer Interactions using AI  
- Long-Term Memory and Personalization  
• Agent to Agent communication  
• Future Directions for Autonomous AI Agents

![](images/ade049795f0d1398c867da68580bc6bd3e8847e76c846cb257f6a136a5bbe5ae.jpg)

<details>
<summary>natural_image</summary>

Cartoon robot character with blue eyes and a small headband, no text or symbols present
</details>

## Building AI Agents

1. Why?  
2. How?  
3. Ingredients?  
4. What can they do?

![](images/6ee6807b3d24baa55f56ee11838971f09e2d5bd9a32d33c84680c76251e6d947.jpg)

<details>
<summary>text_image</summary>

OpenAI
INSIDE
GPT 4
</details>

Key thesis: Humans will communicate with AI using natural language and AI will operate machines allowing for more intuitive and efficient operations

Software 3.0

## AI Agents

1. Why?  
A single call to a large Foundation AI model is not enough. A lot more can be unlocked by building AI systems  
2. How?  
Using model chaining, reflection & other mechanisms  
3. Ingredients?  
Memory, context length, personalization, actions, internet access...  
4. What can they do?

## AI Agents

![](images/46806d85263101a4e84752c9e9aa719ae63b445144a4b5a77460ce884dbc8c63.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Short-term memory"] --> B["Memory"]
  C["Long-term memory"] --> B
  D["Tools"] --> B
  E["Agent"] --> B
  F["Planning"] --> B
  G["Reflection"] --> B
  H["Self-critics"] --> B
  I["Chain of thoughts"] --> B
  J["Subgoal decomposition"] --> B
  K["Search()"] --> D
  L["CodeInterpreter()"] --> D
  M["Calculator()"] --> D
  N["Calendar()"] --> D
  O["...more"] --> D
    B -.-> D
    style B fill:#d3d3d3,stroke:#333
    style D fill:#d3d3d3,stroke:#333
```
</details>

## The first flight to be fully booked by an AI

![](images/2bf472760914e3b8728d23e71dd9fba40f5422d73bdef19179c1f3f9863aee37.jpg)

<details>
<summary>text_image</summary>

New Tab
Search Google or type a URL
Cornell University... Gmail: Email from... Google VC Contacts for E... Mallinator - Let Th... Hacker News Product Hunt Sci-Hub: removin... GitHub does dotfil... term-tools/tmux.c...
MULTI-ON
book a one-way flight from nyc to sfo for June 10 for the MULTION hackathon u Search
4140
</details>

## AI Agent Passing the Online CA Driving Test

![](images/21a789170e7669c8782802f311e4da00bc34c26e882939bb763cfb765bef7d1c.jpg)

<details>
<summary>text_image</summary>

V4.3.79
FVOR Vehicles
What is the appropriate export to drive through a finished route?
○ Submit
△ The product named text.
○ Quitting
Quit Test
Submit
Skip Duration
Microsoft Pro
</details>

Fully Autonomously passing the official DMV online driving test this week & setting the record as the first AI to obtain a driving permit in CA!

## Why human-like AI Agents?

1. Can do what you can do: Able to use existing interfaces designed for humans & operate outside programmatic boundaries  
2. Digital extension of you: Can act as an extension of the user and act on their behalf  
3. Less-restrictive boundaries: Can handle logins, payments, etc. and interact with services without any API restrictions  
4. Simple action space: Need only click & type action primitives  
5. Self-learning: Can learn from the user and self-improve with more interactions

## 5 levels of Autonomy

![](images/cc5954f43f37d291af363c3541f66e1e39735fd3ee5c4424077bc4676d546211.jpg)

<details>
<summary>heatmap</summary>

For on-road vehicles
| Human driver monitors the road | 0 | NO AUTOMATION | Steering and acceleration/deceleration | Monitoring of driving environment | Fallback when automation fails | Automated system is in control |
|---|---|---|---|---|---|---|
| Human driver monitors the road | 1 | DRIVER ASSISTANCE | N/A | N/A | N/A | SOME DRIVING MODES |
| Human driver monitors the road | 2 | PARTIAL AUTOMATION | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES |
| Automated driving system monitors the road | 3 | CONDITIONAL AUTOMATION | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES |
| Automated driving system monitors the road | 4 | HIGH AUTOMATION | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES |
| Automated driving system monitors the road | 5 | FULL AUTOMATION | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES | SOME DRIVING MODES |
</details>

## Computer Interactions

## Agent Computer Interaction

Two routes

![](images/5946beb5c97e30ab85f6779effe0cbbac48dfbacb1528bd222bf5c08230d7fbb.jpg)

<details>
<summary>text_image</summary>

GORILLA
</details>

![](images/d1cec9a50ed52b33e814ec085a65a934e2d75e7d7b8d1cc83caf01085d6db19c.jpg)

<details>
<summary>natural_image</summary>

Abstract circular gradient design with overlapping purple, green, and yellow segments (no text or symbols)
</details>

## API

(programmatic)

easy to build context safer & controllable high variability

## Direct interaction

(browser or desktop control)

easy to take actions

free-form interactions

need to provide guarantees

![](images/8d0fa036d6cf4696424764d9e46b5c1d66f36dd29b1d544dfb48cc9651e2feeb.jpg)

<details>
<summary>natural_image</summary>

Multi-ON logo featuring a stylized atomic or orbital design with gradient colors on a black background (no text or symbols beyond the logo itself)
</details>

ADEPT

## Agent Action API: An universal API for computer interaction

![](images/305a983ffdca8ada9db06f0e961675fcff09d35c595bc328685ef8384d95d6be.jpg)

<details>
<summary>text_image</summary>

import multion
    multion.login()

# Now you can make API calls like this:response = multion.post('https://multion.fly.dev/sessions', {"input": "some input","url": "some url"})
response = multion.new_session(
{"input": f"Make a 50 word tweet announcing that the MULTION API IS LIVE!! And here is the first automated tweet made by AI",
"url": "https://www.twitter.com"
})

response.get('response').get('data').get('message')

# LANGCHAIN EXAMPLE
from langchain import LLMMathChain, OpenAI, SerpAPIWrapper, SQLDatabase, SQLDatabaseChain
from langchain.agents import initialize_agent, Tool
from langchain.agents import AgentType
from langchain.chat_models import ChatOpenAI
import openai

import os
os.environ["LANGCHAIN_TRACING"] = "true"
os.environ['OPENAI_API_KEY'] = "<open_api_key>"
</details>

## Memory & Personalization

## AI Models as Neural Compute Unit

Input Tokens (max token size)

![](images/f50d0a80dbe07bdc324ef8951d3f66360f32d9209a5a0b2c7a7640716ad26626.jpg)

<details>
<summary>text_image</summary>

OpenAI
INSIDE
GPT 4
</details>

MIPS32 Add Immediate Instruction

<table><tr><td>001000</td><td>00001</td><td>00010</td><td>0000000101011110</td></tr><tr><td>OP Code</td><td>Addr 1</td><td>Addr 2</td><td>Immediate value</td></tr></table>

Equivalent mnemonic:

addi \$r1, \$r2, 350

An example simple MIPS32 processor instruction

Output Tokens (max token size)

## AI Models as Neural Compute Unit

![](images/c081ef2c8f37d78e95a37b94382db9fbdfdb5c46d9d3dec0e9edb8e0aed7cc54.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Transformer"] --> B["Scratchpad"]
  A --> C["memory"]
  A --> D["instructions"]
  B --> E["pointers"]
  C --> E
  D --> E
  E --> F["input embedding sequence"]
```
</details>

Looped Transformers

## Long-term Memory

• Works similar to disk (long-lived & persistent)  
- Mechanisms

○ Embeddings  
○ Retrieval models

\- Open Questions:

○ Hierarchy  
○ Temporal Coherence  
○ Structure  
○ Online adaptation

![](images/6b1fc18015e6ee45c9ea0a294e378d415b29625359573d2aab35c9073403efa7.jpg)

<details>
<summary>text_image</summary>

Memory
</details>

## Personalization

- User-Agent Alignment Problem: Enable agent to take actions that are aligned with the user preferences  
• Everyone has different prefs & likes/dislikes:

- Explicit: allergies, favourite dishes, flight seat prefs, ...  
- Implicit: choice between brands, out of 10 items in a listing which user likes better

## Challenges

• Collecting user data & preferences:

- actively asking for preferences  
- passive learning from interactions

- Learning from user preferences: supervised fine-tuning vs human-feedback  
- On-fly adaptation  
- Privacy

## Agent-to-Agent Communication

## Multi Agent Autonomous AI systems

![](images/ea81b167c57e426e821bee069a89ee4b8945aa416123218874b9545b58ec2be4.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot illustration with blue eyes and a small head (no text or symbols)
</details>

![](images/9b5a5666d85c3f9850c8d96c0704bae1a75f7e99bad5dc5edf3ed66f7d3a2ebd.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot illustration with blue eyes and a white head (no text or symbols)
</details>

![](images/1e0fb2fba7849ff187c9d20682ac65a8cf67adbf9c3f9e872f5d50273aa8e8ae.jpg)

<details>
<summary>natural_image</summary>

Cartoon robot character with blue eyes and a small head (no text or symbols)
</details>

![](images/9842096793c6d8d504c077b0da073947e625a74dea4c3a0c489e120e8d1a6239.jpg)

<details>
<summary>natural_image</summary>

Cartoon robot character with blue eyes and a circular head (no text or symbols)
</details>

![](images/79a1da21991746dbaa943c486e380fe91af613a2e46dce5e80d44a7666956281.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot illustration with blue eyes and a small head (no text or symbols)
</details>

## Why Multi-agent Systems

1. Parallelization unlock: Breaking a task into smaller chunks and dividing between agents to improve efficiency & speeds  
2. Task Specialization: An AI agent might seat between the user and each service: e.g. a spreadsheet AI agent, a slack AI agent, a web-browser AI agent, ...  
3. Challenges:

a. Agent to Agent Communication: one AI might want to exchange or request info from another AI agent finish a task

## Agent to Agent Communication

• Exchanging info between fleets of agents  
- Hierarchies  
- Syncing primitives

![](images/240f1eea8aaca59d9b24ebdeb257790351e1b9413651040f245422f30b5a830d.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["User"] --> B["Manager AI Agent"]
  B --> C["Worker AI Agents"]
  B --> D["Worker AI Agents"]
  B --> E["Worker AI Agents"]
```
</details>

## Agent to Agent Communication

\- Robust communication protocols & Syncing primitives: Natural language is ambiguous, need mechanisms to reduce miscommunication!

## Agent to Agent Communication

\- Robust communication protocols & Syncing primitives

Manager state

Task X:

(status: not done)

![](images/20c835eaa3e014661fa7c9dd6b234e08e5b02ae99d357642abdacbf2cd1b8f04.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot character with round head, orange eyes, and brown outline (no text or symbols)
</details>

Manager AI Agent

![](images/8d13a031fb438d2b225bb8083a6d6fabc7e0f0e100b22ace47cd0bf602b64b7c.jpg)  
Worker AI Agents

## Agent to Agent Communication

## - Robust communication protocols & Syncing primitives

Do Task X:

- plan  
- context

Worker State

Task X:
(status: not done)

Manager state

Task X:
(status: not done)

![](images/744c5823a3e44beafc167d43aea2e3fd6ca934c17532f9f223a9492155cfa194.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot head illustration with no text or symbols
</details>

![](images/4ceba14e7b2fb904f02c79f22fcad3506ae5b226d77a4289e65c0e920eb7ff57.jpg)

Worker AI Agents

Manager AI Agent

## Agent to Agent Communication

## - Robust communication protocols & Syncing primitives

Do Task X:

- plan  
- context

Worker State

Task X:
(status: done)

Manager state

Task X:

(status: not done)

![](images/8ea17bbada2a960a154ef54bd9c689e37a70b13e8446ee27ae7d3fac746c590b.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon character with round head, orange eyes, and raised arms (no text or symbols)
</details>

Manager AI Agent

![](images/4242eb5beb06dc719193d40e70907e440b89403bc090c59bcf95b00b05c21ee8.jpg)

Response:

- thought  
- actions  
- status

Worker AI Agents

## Agent to Agent Communication

## - Robust communication protocols & Syncing primitives

Verify if task was correctly done & follows all specifications

Verify Task X:
- required spec

Worker State

Task X: (status: done)

Manager state

Task X:

(status: verify done)

![](images/30da8b19ac28324ca67e491b893a6819b42e2c1f79d513a7b1038f0af7df9faf.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot character with round head, orange eyes, and limbs (no text or symbols)
</details>

![](images/04bb9510cb94e3c4b7516ee5954b81bac56a678beec305965dec5e60e5565a49.jpg)

Manager AI Agent

## Agent to Agent Communication

## - Robust communication protocols & Syncing primitives

Scenario 1:

Task was correctly done & follows all specifications

Verify Task X:

\- required spec

Worker State

Task X:

(status: done)

Manager state

Task X:

(status: verify done)

![](images/94b8c0a934fd0a2671e7819e9db1d68addba3157960b06237ccfb5a617351b2f.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot character with round head, orange eyes, and limbs (no text or symbols)
</details>

![](images/ad4c996aa7e31c365bcc2d584d8c8d14b95e68ad65d854349c115f0ec24d24b7.jpg)

Verify Response:

\- confirm actions correct

![](images/cdf7b1ea95768082d251773721a478c39c926485c11c0cf16fe734d0afef2208.jpg)

Manager AI Agent

## Agent to Agent Communication

\- Robust communication protocols & Syncing primitives

![](images/8d5bded69799561e5713937a64575b33479ddf4842dbcfb6553a5bc4e2e13d8b.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["User"] -->|Task Finished!| B["Manager state"]
  B --> C["Task X: (status: done)"]
  C --> D["Verify Response: confirm actions correct"]
  D --> E["Worker State"]
  E --> F["Task X: (status: done)"]
  F --> G["Verify Task X: required spec"]
  G --> H["Worker State"]
```
</details>

Manager AI Agent

## Agent to Agent Communication

\- Robust communication protocols & Syncing primitives

Scenario 2:

Task was incorrectly done

(Agent Miscommunication)

Verify Task X:

\- required spec

Worker State

Task X:

(status: done)

Manager state

Task X:

(status: verify done)

![](images/195d82be478d57da6b1f538d4e3a39bca511b6142ac325fab6a21a5c8057799e.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot character with round head, orange eyes, and limbs (no text or symbols)
</details>

![](images/cb83fafc1535c050137b5730ee816673744e68ab059bcf568edb812e65f2292a.jpg)

Verify Response:

\- actions were not correct

![](images/31fed1aee8548f1097bb1633a1cf43aeb39c1ffbcf1b6150fa432eae95de7161.jpg)

Manager AI Agent

## Agent to Agent Communication

## - Robust communication protocols & Syncing primitives

Scenario 2:

Task was incorrectly done

(Agent Miscommunication)

Re-do Task X:

- plan  
- context  
- feedback/corrections

Worker State

Task X:

(status: not done)

Manager state

Task X:

(status: not done)

![](images/f2dd8013c7434f589797893f830770addfa6ec2f4f2048dd4b14ef25c57a2884.jpg)

<details>
<summary>natural_image</summary>

Simple cartoon robot head illustration with no text or symbols
</details>

![](images/ac256fcbebad9348f0fe7225f5a1d64ba8a68fc890998737753f78f56d87a2c3.jpg)

Manager AI Agent

## Future Directions

## Key Issues with Autonomous Agents

4. Real world-deployment & Observability

1. Reliability  
2. Looping & Plan Divergence  
3. Testing & Benchmarking

a. How do we trust a fully autonomous AI system

b. How do we build in human overrides

## Plan Divergence

![](images/8c05fd801723ea082d1ee2233cb7a889f69973f8b8bfd508b550d40cfd5f69d4.jpg)

<details>
<summary>line chart</summary>

| Point | Actual Path Value |
|-------|-------------------|
| 1     | 0                 |
| 2     | ~0.8              |
| 3     | ~0.6              |
| 4     | ~0.4              |
| 5     | ~0.7              |
| 6     | ~0.9              |
| 7     | ~0.5              |
| 8     | ~0.8              |
| 9     | ~0.7              |
| 10    | ~0.9              |
| 11    | ~0.6              |
| 12    | ~0.8              |
| 13    | ~0.7              |
| 14    | ~0.9              |
| 15    | ~0.8              |
| 16    | ~0.7              |
| 17    | ~0.9              |
| 18    | ~0.8              |
| 19    | ~0.7              |
| 20    | ~0.9              |
| 21    | ~0.8              |
| 22    | ~0.7              |
| 23    | ~0.9              |
| 24    | ~0.8              |
| 25    | ~0.7              |
| 26    | ~0.9              |
| 27    | ~0.8              |
| 28    | ~0.7              |
| 29    | ~0.9              |
| 30    | ~0.8              |
| 31    | ~0.7              |
| 32    | ~0.9              |
| 33    | ~0.8              |
| 34    | ~0.7              |
| 35    | ~0.9              |
| 36    | ~0.8              |
| 37    | ~0.7              |
| 38    | ~0.9              |
| 39    | ~0.8              |
| 40    | ~0.7              |
| 41    | ~0.9              |
| 42    | ~0.8              |
| 43    | ~0.7              |
| 44    | ~0.9              |
| 45    | ~0.8              |
| 46    | ~0.7              |
| 47    | ~0.9              |
| 48    | ~0.8              |
| 49    | ~0.7              |
| 50    | ~0.9              |
| 51    | ~0.8              |
| 52    | ~0.7              |
| 53    | ~0.9              |
| 54    | ~0.8              |
| 55    | ~0.7              |
| 56    | ~0.9              |
| 57    | ~0.8              |
| 58    | ~0.7              |
| 59    | ~0.9              |
| 60    | ~0.8              |
| 61    | ~0.7              |
| 62    | ~0.9              |
| 63    | ~0.8              |
| 64    | ~0.7              |
| 65    | ~0.9              |
| 66    | ~0.8              |
| 67    | ~0.7              |
| 68    | ~0.9              |
| 69    | ~0.8              |
| 70    | ~0.7              |
| 71    | ~0.9              |
| 72    | ~0.8              |
| 73    | ~0.7              |
| 74    | ~0.9              |
| 75    | ~0.8              |
| 76    | ~0.7              |
| 77    | ~0.9              |
| 78    | ~0.8              |
| 79    | ~0.7              |
| 80    | ~0.9              |
| 81    | ~0.8              |
| 82    | ~0.7              |
| 83    | ~0.9              |
| 84    | ~0.8              |
| 85    | ~0.7              |
| 86    | ~0.9              |
| 87    | ~0.8              |
| 88    | ~0.7              |
| 89    | ~0.9              |
| 90    | ~0.8              |
| 91    | ~0.7              |
| 92    | ~0.9              |
| 93    | ~0.8              |
| 94    | ~0.7              |
| 95    | ~0.9              |
| 96    | ~0.8              |
| 97    | ~0.7              |
| 98    | ~0.9              |
| 99    | ~0.8              |
| 100   | ~0.7              |
</details>

AI Agents like AutoGPT don't know how to correct on making a mistake!

## Karpathy - LLM OS

![](images/612e99b7d0ee2b7e7d50a60b340b6872a4f6b8e42b297afe0072658dd731421d.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Peripheral devices I/O"] --> B["video"]
  A --> C["audio"]
    B <--> D["CPU"]
    C <--> D
    D <--> E["LLM"]
  F["Software 1.0 tools &quot;classical computer&quot;"] --> G["Calculator Python interpreter Terminal ..."]
    G <--> H["LLM"]
  I["Disk"] --> J["File system (+embeddings)"]
    J <--> K["RAM"]
    K <--> L["context window"]
  M["Ethernet"] --> N["Browser"]
    N <--> O["Other LLMs"]
```
</details>

## Building Generalized AI Systems

![](images/9c8cdc8e5877c77b8f280ea5d3fdf8f2974def92a92b43085840381b5ffb2fe3.jpg)

<details>
<summary>natural_image</summary>

Simple stick figure drawing with a circle at the top and three legs forming a V-shape (no text or symbols)
</details>

User

![](images/9b58076ab16d98837b806d3428df31015b689c832b323f81d42a206faa826141.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph TD
  A["Inner thoughts"] --> B["Task Engine"]
  C["Chat Interface"] --> B
  B --> D["Reflection"]
  D --> E["Router"]
  E --> F["Apps/Tools"]
  E --> G["Local Index"]
  H["Web"] --> E
  I["Result"] --> B
  J["Result"] --> E
```
</details>

## Future needs for AI agents

- Error correction mechanisms & better agent frameworks  
• Security & user permission models  
- Sandboxing & deployment in risky settings

## Thats all Folks!