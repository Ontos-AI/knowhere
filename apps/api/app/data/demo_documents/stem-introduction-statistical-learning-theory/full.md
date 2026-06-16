# Class 1 Introduction to Statistical Learning Theory

Carlo Ciliberto
Department of Computer Science, UCL

October 5, 2018

## Administrative Info

▶ Class times: Fridays 14:00 - 15:30 $^{1}$  
▶ Location: Ground Floor Lecture Theater, Wilkins Building $^{2}$  
▶ Office hours: (Time TBA), 3rd Floor Hub room, CS Building, 66 Gower street.  
TA: Giulia Luise  
Website: ccilib er.github.io/intro-stl  
email(s): ccilib er@gmail.com, g.luise.16@ucl.ac.uk  
▶ Workload: 2 assignments (50%) and a final exam (50%). Final exam requires to choose 3 problems out of 6. At least one problem from each “sides” of this course (RKHS or SLT) \*must\* be chosen.

## Course Material

Main resources for the course:

Classes  
Slides

Books and other Resources:

S. Shalev-Shwartz and S. Ben-David Understanding Machine Learning: From Theory to Algorithms (Online Book). Cambridge University Press, 2014.  
▶ O. Bousquet, S. Boucheron and G. Lugosi Introduction to Statistical Learning Theory (Tutorial).  
T. Poggio and L. Rosasco course slides and videos from MIT 9.520: Statistical Learning Theory and Applications.  
P. Liang course notes from Stanford CS229T: Statistical Learning Theory.

## Prerequisites

▶ Linear Algebra: familiarity with vector spaces, matrix operations (e.g. inversion, singular value decomposition (SVD)), inner products and norms, etc.  
▶ Calculus: limits, derivatives, measures, integrals, etc.  
▶ Probability Theory: probability distributions, conditional and marginal distribution, expectation, variance, etc.

## Statistical Learning Theory (SLT)

SLT addresses questions related to:

What does it mean for an algorithm to learn.  
What we can/cannot expect from a learning algorithm.  
▶ How to design computationally & statistically efficient algorithms.  
What to do when a learning algorithm does not work...

SLT studies theoretical quantities that we don't have access to:

It tries to bridge the gap between the unknown functional relations governing a process and our (finite) empirical observations of it.

## Motivations and Examples: Regression

<table><tr><td>Living area (feet $^{2}$ )</td><td>Price (1000$s)</td></tr><tr><td>2104</td><td>400</td></tr><tr><td>1600</td><td>330</td></tr><tr><td>2400</td><td>369</td></tr><tr><td>1416</td><td>232</td></tr><tr><td>3000</td><td>540</td></tr><tr><td>⋮</td><td>⋮</td></tr></table>

![](images/130f421007cfaaea477fe6676367db25b8f408a5fb8193f4e8bd47f92e4e84f4.jpg)

<details>
<summary>scatter plot</summary>

| square feet | price (in $1000) |
| ----------- | ---------------- |
| 1000        | 200              |
| 1200        | 250              |
| 1400        | 300              |
| 1600        | 350              |
| 1800        | 400              |
| 2000        | 450              |
| 2200        | 500              |
| 2400        | 550              |
| 2600        | 600              |
| 2800        | 650              |
| 3000        | 700              |
| 3200        | 650              |
| 3400        | 600              |
| 3600        | 550              |
| 3800        | 500              |
| 4000        | 450              |
| 4200        | 400              |
| 4400        | 350              |
| 4600        | 300              |
| 4800        | 250              |
| 5000        | 200              |
</details>

Image credits: coursera

## Motivations and Examples: Binary Classification

Spam detection: Automatically discriminate spam vs non-spam e-mails.

![](images/933c4cfa11e9b6b09b80ddad813e476c342506f43c76addb08ee2c3572cef92c.jpg)

<details>
<summary>flowchart</summary>

```mermaid
graph LR
  A["Stacked Document"] --> B["{ legit, spam}"]
```
</details>

Image Classification

![](images/899609955dd03e4c21fb21432eab58790a1f67b1c381b757d9380d40b107d30b.jpg)

<details>
<summary>text_image</summary>

f : 
→ {dog, not dog}
</details>

## Motivations and Examples: Multi-class Classification

Identify the category of the object depicted in an image.

Example: Caltech 101

![](images/b73272eefd03c715ce737ff948f4134759e71d88a36eced7bcb7d9a005b51f02.jpg)

## Motivations and Examples: Multi-class Classification

Scaling things up: detect correct object among thousands of categories. ImageNet Large Scale Visual Recognition Challenge

![](images/c49218950f69d09bcbf89b28e7feb5ecb3ba222777df2ace4ac9b97f9487fb88.jpg)

<details>
<summary>text_image</summary>

Grid of numerous emoji icons and text, likely representing a collection of various application or service types.
</details>

## Motivations and Examples: Structured Prediction

## Image Captioning

(also Localization Segmentation Classification)

![](images/25476d2009878f7a2e04bf7c55086a7c487132079fc2fd0594393228659457ae.jpg)

<details>
<summary>natural_image</summary>

Cat sitting at a desk with papers and a cup, viewed through a window with curtains (no visible text or symbols)
</details>

![](images/2c299d79f71e9b33d82df388ffa2c92c0454718c9def6fcfa7bf9166786c36b4.jpg)

<details>
<summary>natural_image</summary>

A cat sitting at a desk with a book, viewed through a window (no visible text or symbols)
</details>

Gray striped cat

Business suit

Coffee mug

Newspaper

Movie Ranking

NETFLIX

user:127

![](images/b459366b99a647b3228e24d712bdbed7960a5000d2636990f6f9ae69b333301d.jpg)

![](images/46f73a7eab5bea2922df3ec9989cc3d858e8b9dd5280086ea72441d93e85f638.jpg)

![](images/e8bb55e54ca29d04d82c2bf1b521cc63a2cbedd0a86db911cb99d08d4b54a1ce.jpg)

Speech Recognition "Ok Google"

![](images/e6f03e84ca7dcee4c061f343371cc6b235116ee114ce0172b82464b7144cd8f2.jpg)

<details>
<summary>natural_image</summary>

Abstract blue waveform visualization with no text or symbols
</details>

Protein Folding

![](images/b01a930ba66ad1ecf8aafc1df14ffd85b78f8105dafa245723557e1d712dfba9.jpg)

![](images/f222c147bee68cd28f4291bef0775a0f8335ca96ad13eaf890dfc15c074d0043.jpg)

## Formulating The Learning Problem

## Formulating the Learning Problem

## Main ingredients:

▶ X input and Y output spaces.  
▶ ρ unknown distribution on $X \times Y$ .  
$\ell : \mathcal{Y} \times \mathcal{Y} \to \mathbb{R}$ a loss function measuring the discrepancy $\ell(y, y')$ between any two points $y, y' \in \mathcal{Y}$ .

We would like to minimize the expected risk

$$
\underset {f: \mathcal {X} \to \mathcal {Y}} {\text { minimize }}   \mathcal {E} (f) \quad \mathcal {E} (f) = \int_ {\mathcal {X} \times \mathcal {Y}} \ell (f (x), y) d \rho (x, y)
$$

The expected prediction error incurred by a predictor $^{3}$ $f : X \to Y$ .

## Input Space

## Linear Spaces

Vectors  
Matrices  
Functions

## "Structured" Spaces

▶ Strings  
▶ Graphs  
Probabilities  
▶ Points on a manifold  
▶ ...

## Output Space

## Linear Spaces, e.g.

▶ $\mathcal{Y} = R$ regression  
▶ $Y = \{1, \ldots, T\}$ classification  
$\mathcal{Y} = \mathbb{R}^{T}$ multi-task

## "Structured" Spaces, e.g.

▶ Strings  
Graphs  
Probabilities  
Orders (i.e. Ranking)  
▶ ...

## Probability Distribution

Informally: the distribution $\rho$ on $X \times Y$ encodes the probability of getting a pair $(x, y) \in \mathcal{X} \times \mathcal{Y}$ when observing (sampling from) the unknown process.

Throughout the course we will assume $\rho(x,y)=\rho(y|x)\rho_{\mathcal{X}}(x)$

▶ $\rho_{\mathcal{X}}(x)$ marginal distribution on X.  
▶ $\rho(y|x)$ conditional distribution on Y given $x \in X$ .

## Conditional Distribution

$\rho(y|x)$ characterizes the relation between a given input x and the possible outcomes y that could be observed.

In noisy settings it represents the uncertainty in our observations.

Example: $y = f_{*}(x) + \epsilon$ , with $f_{*} : X \to R$ the “true” function and $\epsilon \sim \mathcal{N}(0, \sigma)$ Gaussian distributed noise. Then:

$$
\rho (y | x) = \mathcal {N} (f _ {*} (x), \sigma)
$$

# Loss Functions

The loss function

$$
\ell : \mathcal {Y} \times \mathcal {Y} \rightarrow [ 0, + \infty)
$$

represents the cost $\ell(f(x), y)$ incurred when predicting $f(x)$ instead of y.

It is part of the problem formulation:

$$
\mathcal {E} (f) = \int \ell (f (x), y) d \rho (x, y)
$$

The minimizer of the risk (if it exists) is “chosen” by the loss.

# Loss Functions for Regression

$$
L (y, y ^ {\prime}) = L (y - y ^ {\prime})
$$

▶ Square loss $L(y, y') = (y - y')^{2}$ ,  
▶ Absolute loss $L(y, y') = |y - y'|$ ,  
▶ ε-insensitive $L(y, y') = \max(|y - y'| - \epsilon, 0)$ ,

![](images/154a0bee1083f4c26439233b1afe778c7b25a0d7626c8cb51f3e20bdf8dff08f.jpg)

<details>
<summary>line chart</summary>

| x    | Square Loss | Absolute | ε - insensitive |
| ---- | ----------- | -------- | --------------- |
| 1.0  | 1.0         | 1.0      | 0.7             |
| 0.5  | 0.2         | 0.4      | 0.2             |
| 0.0  | 0.0         | 0.0      | 0.0             |
| 0.5  | 0.2         | 0.4      | 0.2             |
| 1.0  | 1.0         | 1.0      | 0.7             |
</details>

# Loss Functions for Classification

$$
L (y, y ^ {\prime}) = L (- y y ^ {\prime})
$$

▶ 0-1 loss $L(y, y') = \mathbf{1}_{\{-yy' > 0\}}$  
▶ Square loss $L(y, y') = (1 - yy')^{2}$ ,  
▶ Hinge-loss $L(y, y') = \max(1 - yy', 0)$ ,  
▶ logistic loss $L(y, y') = \log(1 + \exp(-yy'))$ ,

![](images/ee108933fe14119599a5efa57665e51009c4875b53d10c79e157a603e1e1575b.jpg)

<details>
<summary>line chart</summary>

| x    | Line 1 | Line 2 | Line 3 | Line 4 |
| ---- | ------ | ------ | ------ | ------ |
| 0    | 1.0    | 1.0    | 1.0    | 1.0    |
| 1    | 0.0    | 0.0    | 0.0    | 0.0    |
| 2    | 1.0    | 0.5    | 0.2    | 0.3    |
</details>

— 01 loss  
— square loss  
— Hinge loss  
— Logistic loss

## Formulating the Learning Problem

The relation between X and Y encoded by the distribution $\rho$ is unknown in reality. The only way we have to access a phenomenon is from finite observations.

The goal of a learning algorithm is therefore to find a good approximation $f_{n}: X \to Y$ for the minimizer of expected risk

$$
\inf _ {f: \mathcal {X} \to \mathcal {Y}} \mathcal {E} (f)
$$

from a finite set of examples $(x_{i}, y_{i})_{i=1}^{n}$ sampled independently from $\rho$ .

## Defining Learning Algorithms

Let $S = \bigcup_{n \in N} (\mathcal{X} \times \mathcal{Y})^n$ be the set of all finite datasets on $\mathcal{X} \times \mathcal{Y}$ . Denote F the set of all measurable functions $f: \mathcal{X} \to \mathcal{Y}$ . A learning algorithm is a map

$$
\begin{array}{l} A: \mathcal {S} \to \mathcal {F} \\ S \mapsto A (S): \mathcal {X} \to \mathcal {Y} \\ \end{array}
$$

To highlight our interest in studying the relation between the size of a training set $S = (x_{i}, y_{i})_{i=1}^{n}$ and the corresponding predictor produced by an algorithm A, we will often denote (with some abuse of notation)

$$
f _ {n} = A \left(\left(x _ {i}, y _ {i}\right) _ {i = 1} ^ {n}\right)
$$

## Non-deterministic Learning Algorithms

We can also consider stochastic algorithms, where the estimator $f_{n}$ is not automatically determined by the training set.

In these cases, given a dataset $S \in S$ , an algorithm $A(S)$ can be seen as a distribution on $\mathcal{F}$ and its output is one sample from $A(S)$ .

Under this interpretation a deterministic algorithm corresponds to $A(S)$ being a Dirac's delta.

# Formulating the Learning Problem

Given a training set, we would like a learning algorithm to find a “good” predictor $f_{n}$ .

What does “good” mean? That it has small error (or excess risk) with respect to the best solution of the learning problem.

Excess Risk

$$
\mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f)
$$

## The Elements of Learning Theory

## Consistency

Ideally we would like the learning algorithm to be consistent

$$
\lim _ {n \to + \infty} \mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) = 0
$$

Namely that (asymptotically) our algorithm “solves” the problem.

However $f_{n}=A(S)$ is a random variable: the points in the training set $S=(x_{i},y_{i})_{i=1}^{n}$ are randomly sampled from $\rho$ .

So what do we mean by $\mathcal{E}(f_{n})\to\inf\mathcal{E}(f)$ ?

## Convergence of Random Variables

Convergence in expectation:

$$
\lim _ {n \to + \infty} \mathbb {E} \left[ \mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) \right] = 0
$$

Convergence in probability:

$$
\lim _ {n \rightarrow + \infty} \mathbb {P} \left(\mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) > \epsilon\right) = 0 \quad \forall \epsilon > 0
$$

Many other notions of convergence of random variables exist!

## Consistency vs Convergence of the Estimator

Note that we are only interested in guaranteeing that the risk of our estimator will converge to the best possible value

$$
\mathcal {E} (f _ {n}) \to \inf _ {f \in \mathcal {F}} \mathcal {E} (f)
$$

but we are not directly interested in determining whether $f_{n} \to f^{*}$ (in some norm) where $f^{*}: X \to Y$ is a minimizer of the expected risk

$$
\mathcal {E} (f ^ {*}) = \inf _ {f: \mathcal {X} \to \mathcal {Y}} \mathcal {E} (f)
$$

Actually, the risk could even not admit a minimizer $f^{*}$ (although typically it will).

This is a main difference with several settings such as compressive sensing and inverse problems.

## Existence of a Minimizer for the Risk

However, the existence of $f^{*}$ can be useful in several situations.

Least Squares. $\ell(f(x),y)=(f(x)-y)^{2}$ . Then

$$
\mathcal {E} (f) - \mathcal {E} (f ^ {*}) = \| f - f ^ {*} \| _ {L ^ {2} (\mathcal {X}, \rho)}
$$

Lipschitz Loss. $|\ell(z,y)-\ell(z',y)|\leq L|z-z'|$

$$
\mathcal {E} (f) - \mathcal {E} (f ^ {*}) \leq L \| f - f ^ {*} \| _ {L ^ {1} (\mathcal {X}, \rho)}
$$

Convergence $f_{n} \rightarrow f^{*}$ (in $L^{1}$ or $L^{2}$ norm respectively) automatically guarantees consistency!

## Measuring the “Quality” of a Learning Algorithm

Is consistency enough? Well no. It does not provide a quantitative measure of how “good” a learning algorithm is.

In other words, question: how do we compare two learning algorithms?

Answer: via their Learning Rates, namely the “speed” at which the excess risk goes to zero as n increases.

Example: Expectation

$$
\mathbb {E} \left[ \mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) \right] = O (n ^ {- \alpha}) \quad \text { for   some } \alpha > 0.
$$

We can compare two algorithms by determining which one has a faster learning rate (i.e. larger exponent $\alpha$ ).

## Sample Complexity, Error Bounds and Tail Bounds

Sample Complexity: minimum number $n(\epsilon, \delta)$ of training points the algorithm needs to achieve an excess risk lower than $\epsilon$ with at least probability $1 - \delta$ :

$$
\mathbb {P} \left(\mathcal {E} (f _ {n (\epsilon , \delta)}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) \leq \epsilon\right) \geq 1 - \delta
$$

Error Bounds: Upper bound $\epsilon(\delta,n)>0$ on the excess risk of $f_{n}$ which holds with probability larger than $1-\delta$

$$
\mathbb {P} \left(\mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) \leq \epsilon (\delta , n)\right) \geq 1 - \delta
$$

Tail Bounds: Lower bound $\delta(\epsilon,n)\in(0,1)$ on the probability that $f_{n}$ will have excess risk larger than $\epsilon$

$$
\mathbb {P} \left(\mathcal {E} (f _ {n}) - \inf _ {f \in \mathcal {F}} \mathcal {E} (f) \leq \epsilon\right) \geq 1 - \delta (\epsilon , n)
$$

## Empirical Risk as a Proxy

If $\rho$ is unknown... how can we say anything about $\mathcal{E}(f_n) - \inf_{f\in \mathcal{F}}\mathcal{E}(f)?$

We have “glimpses” of $\rho$ only via the samples $(x_{i}, y_{i})_{i=1}^{n}$ . Can we use them to gather some information about $\rho$ (or better, on $\mathcal{E}(f)$ )?

Consider function $f : X \to Y$ and its empirical risk

$$
\mathcal {E} _ {n} (f) = \frac {1}{n} \sum_ {i = 1} ^ {n} \ell (f (x _ {i}), y _ {i})
$$

A simple calculation shows that

$$
\mathbb {E} _ {S \sim \rho^ {n}} (\mathcal {E} _ {n} (f)) = \frac {1}{n} \sum_ {i = 1} ^ {n} \mathbb {E} _ {(x _ {i}, y _ {i}) \sim \rho} (\ell (f (x _ {i}), y _ {i})) = \frac {1}{n} \sum_ {i = 1} ^ {n} \mathcal {E} (f) = \mathcal {E} (f)
$$

The expectation of $\mathcal{E}_{n}(f)$ is the expected risk $\mathcal{E}(f)!$

# Empirical Vs Expected

How close is $\mathcal{E}_{n}(f)$ to $\mathcal{E}(f)$ with respect to the number n of training points?

Consider i.i.d. random variables X and $(X_{i})_{i=1}^{n}$ . Let $\bar{X}_{n} = \frac{1}{n} \sum_{i=1}^{n} X_{i}$ . Then

$$
\mathbb {E} [ (\bar {X} _ {n} - \mathbb {E} (X)) ^ {2} ] = \operatorname{Var} (\bar {X} _ {n}) = \frac {\operatorname{Var} (X)}{n}
$$

Therefore the expected (squared) distance between the empirical mean of the $X_{i}$ and their expectation $\mathbb{E}(X)$ goes to zero as $O(1/n)$ (Assuming X to have finite variance).

If $X_{i} = \ell (f(x_{i}),y_{i})$ , we have $\bar{X}_n = \mathcal{E}_n(f)$ and therefore

$$
\mathbb {E} [ (\mathcal {E} _ {n} (f) - \mathcal {E} (f)) ^ {2} ] = \frac {\operatorname{Var} (\ell (f (x) , y))}{n}
$$

## Empirical Vs Expected Risk

If $X_{i} = \ell(f(x_{i}), y_{i})$ , we have $\bar{X}_{n} = \mathcal{E}_{n}(f)$ and therefore

$$
\mathbb {E} [ (\mathcal {E} _ {n} (f) - \mathcal {E} (f)) ^ {2} ] = \frac {\mathrm{Var} (\ell (f (x) , y))}{n}
$$

In particular

$$
\mathbb {E} [ | \mathcal {E} _ {n} (f) - \mathcal {E} (f) | ] \leq \sqrt {\frac {\mathrm{Var} (\ell (f (x) , y))}{n}}
$$

## Empirical Vs Expected

Assume for simplicity that there exists a minimizer $f_{*}: X \to Y$ of the expected risk

$$
\mathcal {E} (f _ {*}) = \inf _ {f \in \mathcal {F}} \mathcal {E} (f)
$$

For any function $f : X \to Y$ we can decompose the excess risk as

$$
\begin{array}{l} \mathcal {E} (f) - \mathcal {E} (f _ {*}) = \\ \mathcal {E} (f) - \mathcal {E} _ {n} (f) + \mathcal {E} _ {n} (f) - \mathcal {E} _ {n} (f _ {*}) + \mathcal {E} _ {n} (f _ {*}) - \mathcal {E} (f _ {*}), \\ \end{array}
$$

recalling the definition $\mathcal{E}_{n}(f):=\frac{1}{n}\sum_{i=1}^{n}\ell(f(x_{i}),y_{i})$ of the empirical risk. Note that this in particularly then also holds for $f_{n}$ , which we will use below. We can therefore leverage on the statistical relation between $E_{n}$ and E to study the expected risk in terms of the empirical risk.

This perspective leads to one of the most well-established strategies on SLT: Empirical Risk Minimization

## Empirical Risk Minimization

Let $f_{n}$ be the minimizer of the empirical risk

$$
f _ {n} = \underset {f \in \mathcal {F}} {\operatorname{argmin}} \mathcal {E} _ {n} (f)
$$

Then we automatically have $\mathcal{E}_{n}(f_{n}) - \mathcal{E}_{n}(f_{*}) \leq 0$ (for any choice of training set).

Then

$$
\mathbb {E} \mathcal {E} (f _ {n}) - \mathcal {E} (f _ {*}) \leq \mathbb {E} \mathcal {E} (f _ {n}) - \mathcal {E} _ {n} (f _ {n}) \quad (\text { why? })
$$

We can focus on studying only the generalization error

$$
\mathbb {E} \mathcal {E} (f _ {n}) - \mathcal {E} _ {n} (f _ {n})
$$

# Generalization Error

How can we control the generalization error

$$
\mathcal {E} _ {n} (f _ {n}) - \mathcal {E} (f _ {n})
$$

with respect to the number n of examples?

This question is far from trivial...

(and it is one of the main subject of SLT)

Indeed, $E_{n}$ and $f_{n}$ both depend on the sampled training data. Therefore, we cannot use the result

$$
\mathbb {E} \left[ \left. \left| \mathcal {E} _ {n} (f _ {n}) - \mathcal {E} (f _ {n}) \right| \right] \leq O (1 / \sqrt {n}) \right.
$$

which indeed will not be true in general... (next class).

## A Taxonomy of Supervised Learning Problems

## A Taxonomy of Supervised Learning Problems

In practice we can have many different problems and scenarios:

Parametric Vs Non-parametric learning  
▶ Fixed design Vs random design  
▶ Transductive Vs inductive learning  
▶ Offline/batch Vs online/adversarial learning

Different goals and assumptions but similar tools to study/solve them!

## Parametric Vs Non-parametric

How much do we know about the model?

▶ Parametric: assume the predictor to be modeled by a finite number of unknown parameters. Goal: find the parametrization that best fits the observed data. In several scenario the goal is not in (only) having good predictions but rather use the recovered model for other purposes (e.g. identification).  
▶ Non-parametric. allow the parametrization of the model to increase in complexity as more examples are observed. Goal: find an estimator with optimal generalization performance (i.e. lowest expected risk E).

## Fixed Design Vs Random Design

From experiment design...

▶ Fixed Design. Given training examples $(x_{i}, y_{i})_{i=1}^{n}$ , the goal is to achieve good estimates for $\rho(y|x_{i})$ on the prescribed training inputs. No distribution on the input data $\rho_{X}$ is assumed/considered.

$$
\frac {1}{n} \sum_ {i = 1} ^ {n} \int_ {\mathcal {Y}} \ell (f (x _ {i}), y) d \rho (y | x _ {i})
$$

▶ Random Design. Agnostic about where the learned model will be tested. The goal is to make good predictions with respect to the distribution $\rho(x,y)$ .

## Inductive Vs Transductive Learning

Do we have access to the test set in advance?

Transductive: the goal is to achieve good prediction performance on a prescribed set of test points $(\tilde{x}_{j})_{j=1}^{n_{test}}$ provided in advance. Transductive learning ignores the effect of $\rho_{X}$ on the risk but focuses only on

$$
\frac {1}{n _ {t e s t}} \sum_ {j = 1} ^ {n _ {t e s t}} \int_ {\mathcal {Y}} \ell (f (\tilde {x} _ {j}), y) d \rho (y | \tilde {x} _ {j})
$$

▶ Inductive Agnostic about where the learned model will be tested. The goal is to make good predictions with respect to the distribution $\rho(x, y)$ .

## Offline/Batch Vs Online/Adversarial Learning

How do we observe samples from $\rho$ ?

▶ Offline/Batch: a finite sample of input-output examples independently and identically distributed. Goal: minimize prediction errors on new examples  
▶ Online/Adversarial: We observe one input, propose a prediction and then observe the output. Goal: minimize the regret (i.e. choose the estimator that would have made less mistakes).

Note. The distribution could be adversarial: $\rho(y|x, f(x))$ instead of $\rho(y|x)$ can make things “hard” for us.

## Wrapping up

## This class:

▶ Motivations and Examples  
Formulating the learning problem  
▶ Brief introduction to Learning Theory  
▶ A Taxonomy of supervised learning problems

Next class: overfitting and the need for regularization...