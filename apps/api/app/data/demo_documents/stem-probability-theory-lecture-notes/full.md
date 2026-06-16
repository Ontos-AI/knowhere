## Probability Theory Lecture Notes

Phanuel Mariano

## Contents

## Chapter 1. Combinatorics 5

1.1. Counting Principle 5  
1.2. Permutations 6  
1.3. Combinations 7  
1.4. Multinomial Coefficients 9

## Chapter 2. Axioms of Probability 10

2.1. Sample Space and Events 10  
2.2. Axioms of Probability 12  
2.3. Equally Likely Outcomes 15

## Chapter 3. Independence 18

3.1. Independent Events 18

## Chapter 4. Conditional Probability and Independence 22

4.1. Conditional Probabilities 22  
4.2. Bayes's Formula 26

## Chapter 5. Random Variables 29

5.1. Random Variables 29  
5.2. Discrete Random Variables 31  
5.3. Expected Value 32  
5.4. The C.D.F. 34  
5.5. Expected Value of Sums of Random Variables 37  
5.6. Expectation of a Function of a Random Variable 39  
5.7. Variance 41

## Chapter 6. Some Discrete Distributions 43

6.1. Bernouli and Binomial Random Variables 43  
6.2. The Poisson Distribution 46  
6.3. Other Discrete Distributions 49

## Chapter 7. Continuous Random Variables 53

7.1. Intro to continuous R.V 53  
7.2. Expectation and Variance 55  
7.3. The uniform Random Variable 58  
7.4. More practice 59

## Chapter 8. Normal Distributions 60

## CONTENTS

8.1. The normal distribution 60

Chapter 9. Normal approximations to the binomial 64

9.1. The normal approximates Binomial 64

Chapter 10. Some continuous distributions 66

10.1. Exponential Random Variables 66

10.2. Other Continuous Distributions 69

10.3. The distribution function of a Random variable 70

Chapter 11. Multivariate distributions 74

11.1. Joint distribution functions 74

11.2. Independent Random Variables 78

11.3. Sums of independent Random Variables( $\star$ ) 80

11.4. Conditional Distributions- Discrete( $\star$ ) 82

11.5. Conditional Distributions- Continuous( $\star$ ) 83

11.6. Joint PDF of functions 84

Chapter 12. Expectations 87

12.1. Expectation of Sums of R.V. 87

12.2. Covariance and Correlations. 90

Chapter 13. Moment generating functions 93

13.1. Moment Generating Functions 93

Chapter 14. Limit Laws 97

14.1. The Central Limit Theorem 97

## CHAPTER 1

## Combinatorics

## 1.1. Counting Principle

\- We need a way to help us count faster rather than counting by hand one by one.

FACT. (Basic Counting Principle) Suppose 2 experiments are to be performed.

If one experiment can result in m possibilities

Second experiment can result in $n$ possibilities

Then together there are mn possibilities

- I like to use the box method. For example. Each box represent the number of possibilities in that experiment.  
- Example1: There are 20 teachers and 100 students in a school. How many ways can we pick a teacher and student of the year?

\- Solution: Use the box Method: $20 \times 100 = 2000$ .

- The counting principle can be generalized to any amount of experiments: $n_1 \cdots n_r$ possibilities  
- Example2:

- A college planning committee consists of 3 freshmen, 4 sophomores, 5 juniors, and 2 seniors.  
- A subcommittee of 4 consists 1 person from each class. How many?  
- Solution: Box method $3 \times 4 \times 5 \times 2 = 120$ .

\- Example3: How many differen 6-place license plates are possible if the first 3 places are to be occupied by letters and the finals 3 by numbers?

- Solution: $26 \cdot 26 \cdot 26 \cdot 10 \cdot 10 \cdot 10 = ?$  
- Question: What if no repetition is allowed?  
- Solution: $26 \cdot 25 \cdot 24 \cdot 10 \cdot 9 \cdot 8$

\- Example4: How many functions defined on $n$ points are possible if each functional value is either 0 or 1.

\- Solution: Box method on the $1, \ldots, n$ points gives us $2^n$ possible functions.

## 1.2. PERMUTATIONS

## 1.2. Permutations

\- How many different ordered arrangements of the letters $a, b, c$ are possible?

\- $abc, acb, bac, bca, cab$ Each arrangement is a permutation

\- Can also use the box method to figure this out: $3 \cdot 2 \cdot 1 = 6$ .

FACT. With $n$ objects. There are

$$
n (n - 1) \dots 3 \cdot 2 \cdot 1 = n!
$$

different permutations of the $n$ objects.

(★) Note that ORDER matters when it comes to Permutations

\- Example1: What is the numnber of possible batting order with 9 players?

\- Answer: 9!(Box Method or permutations)

\- Example2: How many ways can one arrange 4 math books, 3 chemistry books, 2 physics books, and 1 biology book on a bookshelf so that all the math books are together, all the chemistry books are together, and all the physics books are together.

\- Answer: We can arrange the math books in 4! ways, the chemistry in 3! ways, the physics in 2! ways, and B in 1! = 1 way.

\- But we also have to decide which set of books go on the left, which next, and so on. That is the same as the number of ways of arranging the letters M,C, P,B, and there are 4! ways of doing that. MCPB, PBPB ect..

\- So 4! (4!3!2!1!) ways.

\- Example3: Repetitions: How many ways can one arrange the letters $a, a, b, c$ ?

\- Let us label them $A, a, b, c$ . There are 4!, or 24, ways to arrange these letters. But we have repeats: we could have $Aa$ or $aA$ . So we have a repeat for each possibility (so divide!!!), and so the answer should be $4! / 2! = 12$ .

\- If there were 3 $a's$ , 4 $b's$ , and 2 $c's$ , we would have

$$
\frac {9 !}{3 ! 4 ! 2 !}
$$

\- Example4: How many different letter arrangements can be formed from the word PEPPER?

\- Answer: There 3 $P$ 's 2 $E$ 's and one $R$ . So $\frac{6!}{3!2!1!} = 30$ .

FACT. There are

$$
\frac {n !}{n _ {1} ! \cdots n _ {r} !}
$$

different permutations of $n$ objects of which $n_1$ are alike, $n_2$ are alike, $n_r$ are alike.

\- Example4: Suppose there are 4 Czech tennis players, 4 U.S. players, and 3 Russian players, in how many ways could they be arranged?

\- Answer: $\frac{11!}{4!4!3!}$ .

## 1.3. Combinations

\- We are often interested in selecting $r$ objects from a total of $n$ objects.

\- How many ways can we choose 3 letters out of 5? (Does order matter here? NO) If the letters are $a, b, c, d, e$ then there would be 5 for the first position, 4 for the second, and 3 for the third, for a total of $5 \times 4 \times 3$ . But order doesn't matter here. So we're over counting here....

\- But suppose the letters selected were a, b, c. If order doesn't matter, we will have the letters $a, b, c \ 3! = 6$ times, because there are $3!$ ways of arranging a group of 3. The same is true for any choice of three letters. So we should have

$$
\frac {5 \cdot 4 \cdot 3}{3 !} = \frac {5 !}{3 ! 2 !} = 1 0.
$$

Or what we did was $5 \cdot 4$ , or $n(n-1) \cdots (n-r+1)$ then divided by the repeats 3!.

\- This is often written $\left( \begin{array}{l}5\\ 3 \end{array} \right)$ , read "5 choose 3". More generally..

FACT. If $r \leq n$ , then

$$
\binom{n}{r} = \frac {n !}{(n - r) ! r !}
$$

and say $n$ choose $r$ , represents the number of possible combinations of objects taken $r$ at a time.

(★) Order DOES NOT Matter here

\- Recall in Permutations order did matter.

\- Example1: How many ways can one choose a committee of 3 out of 10 people?

\- Answer: $\left( \begin{array}{l}10\\ 3 \end{array} \right) = \frac{10!}{3!7!} = \frac{10\cdot 9\cdot 8}{3\cdot 2} = 10\cdot 3\cdot 4 = 120.$

\- Example2: Suppose there are 9 men and 8 women. How many ways can we choose a committee that has 2 men and 3 women?

\- $\underline{\text{Answer:}}$ We can choose 2 men in $\binom{9}{2}$ ways and 3 women in $\binom{8}{3}$ ways. The number of committees is then the product $\binom{9}{2} \cdot \binom{8}{3}$ .

\- Example3:A person has 8 friends, of whom 5 will be invited to a party. (We've all been through this)

\- (a) How many choices are there if 2 of the friends are feuding and will not attend together?

\* Box it: [none] + [one of them] [others]

\* $\binom{6}{5}+\binom{2}{1}\cdot\binom{6}{4}$ (recall that when we have OR, use +)

\- (b) How many choices if 2 of the friends will only attend together?

\* Box it: [none] + [with both]

\* $\binom{6}{5}+1\cdot1\cdot\binom{6}{3}$

\- The value of $\left( \begin{array}{l}n\\ r \end{array} \right)$ are called binomials coefficients because of their prominence in the binomial theorem.

THEOREM. (The Binomial Theorem)

$$
(x + y) ^ {n} = \sum_ {k = 0} ^ {n} \binom{n}{k} x ^ {k} y ^ {n - k}.
$$

PROOF. To see this, the left hand side is $(x+y)(x+y)\cdots(x+y)$ . This will be the sum of $2^{n}$ terms, and each term will have n factors. How many terms have k x's and n-k y's? This is the same as asking in a sequence of n positions, how many ways can one choose k of them in which to put x's? (Box it) The answer is $\binom{n}{k}$ , so the coefficient of $x^{k}y^{n-k}$ should be $\binom{n}{k}$ . ☐

• Example: Expand $(x + y)^{3}$ .

$$
- \underline {{\text { Solution: }}} (x + y) ^ {3} = y ^ {3} + 3 x y ^ {2} + 3 x ^ {2} y + x ^ {3}.
$$

\- Problem: Using Combinatorics: Let's prove

$$
\binom{1 0}{4} = \binom{9}{3} + \binom{9}{4}
$$

with no algebra:

\- The LHS represents the number of committees having 4 people out of the 10.

\- Let's say the President of the university will be in one of these committees and he's special, so we want to know when he'll be there or not.

\- When he's there, then there are $1 \cdot \begin{pmatrix} 9 \\ 3 \end{pmatrix}$ is the number of ways that contain the President while $\begin{pmatrix} 9 \\ 4 \end{pmatrix}$ is the number of committees that do not contain the President and contain 4 out of the remaining people.

• The more general equation is

$$
\binom{n}{r} = \binom{n - 1}{r - 1} + \binom{n - 1}{r}
$$

## 1.4. Multinomial Coefficients

\- Example: Suppose one has 9 people and one wants to divide them into one committee of 3, one of 4, and a last of 2. How many different ways are there?

\- Solution: (Box it) There are $\left( \begin{array}{l}9\\ 3 \end{array} \right)$ ways of choosing the first committee. Once that is done, there are 6 people left and there are $\left( \begin{array}{l}6\\ 4 \end{array} \right)$ ways of choosing the second committee. Once that is done, the remainder must go in the third committee. So there is 1 one to choose that. So the answer is

$$
\frac {9 !}{3 ! 6 !} \frac {6 !}{4 ! 2 !} = \frac {9 !}{3 ! 4 ! 2 !}.
$$

\- In general: Divide $n$ objects into one group of $n_1$ , one group of $n_2$ , ... and a $k$ th group of $n_k$ , where $n = n_1 + \cdots + n_k$ , the answer is there are

$$
\frac {n !}{n _ {1} ! n _ {2} ! \cdots n _ {k} !} \text { ways. }
$$

• These are known as multinomial coefficients. We write them as

$$
\binom{n}{n _ {1}, n _ {2}, \ldots , n _ {k}} = \frac {n !}{n _ {1} ! n _ {2} ! \cdots n _ {k} !}.
$$

\- Example: Suppose we are to assign Police officers their duties. Out of 10 officers: 6 patrols, 2 in station, 2 in schools.

$$
- \underline {{\text { Answer }}}: \frac {1 0 !}{6 ! 2 ! 2 !}.
$$

\- Example: There are 10 flags:5 indistinguishable Blue flags, 3 indistinguishable Red flags, and 2 indistinguishable Yellow flags. How may different ways can we order them on a flag pole?

$$
- \underline {{\text {Answer:}}} \frac {1 0 !}{5 ! 3 ! 2 !}.
$$

\- Example: Suppose one has 8 indistinguishable balls. How many ways can one put them in 3 boxes?

\- Solution1: Let us make sequences of o's and |'s; any such sequence that has | at each side, 2 other |'s, and 8 o's represents a way of arranging balls into boxes. For example, if one has

$$
\left| \begin{array}{c c c} o o & 0 0 0 & 0 0 0 \end{array} \right|.
$$

\- How many different ways can we arrange this where we have start with $|$ and end with $|$ . In between, we are only arranging $8 + 2 = 10$ symbols, of which only 8 are $o$ 's

\- So the question is: How many ways out of 10 spaces can one pick 8 of them into which to put an $o$ ?

$$
- \binom{1 0}{8}.
$$

\- Solution2: Look at spaces between. There are 9 spaces. So $\left( \begin{array}{l}9\\ 2 \end{array} \right) + 9$ .

# CHAPTER 2

# Axioms of Probability

## 2.1. Sample Space and Events

\- We will have a sample space, denoted $S$ (sometimes $\Omega$ , or $\mathcal{U}$ ) that consists of all possible outcomes from an experiment.

## - Example1:

\* Experiment: Roll two dice,  
\* Sample Space: $S =$ would be all possible pairs made up of the numbers one through six. List it here. $\{(i,j):i,j = 1,\dots 6\}$ . 36 points.

## - Example 2:

\* Experiment: Toss a coin twice  
\* $S = \{HH,HT,TH,TT\}$

## - Example3:

\* Experiment: Measuring the number of accidents of a random person before they had turn 18.

$$
\cdot S = \{0, 1, 2, \dots \}
$$

## - Others:

\* Let $S$ be the possible orders in which 5 horses finish in a horse race;  
\* Let $S$ be the possible price of some stock at closing time today; or $S = [0, \infty)$ ;  
\* The age at which someone dies, $S = [0, \infty)$ .

\- Events: An event $A$ is a subset of $S$ . In this case we use the notation $A \subset S$ , to mean $A$ is a subset of $S$ .

- $A \cup B$ : points in $S$ such that is in $A$ OR $B$ OR BOTH.  
- $A \cap B$ , points in $A$ AND $B$ . (you may also see $AB$ )  
- $A^c$ is the compliment of $A$ , the points NOT in $A$ . (you may also see $A'$ )  
- Can extend to $A_1, \ldots, A_n$ events. $\bigcup_{i=1}^{n} A_i$ and $\bigcap_{i=1}^{n} A_i$ .

![](images/6b3f0ded4cd051c630042135580e78684c6922459b11c4e8aa2caecb8c45b6d5.jpg)

![](images/4d7245c4b763b1a984167fda64161a505bddabac420c7708955314935e85d98a.jpg)

![](images/2b501a36ebecf8e6510d38ab34de1a9cde321b71f7e70a0f69c6dbbc4e4e4ddf.jpg)

![](images/0980c5188cef806986a7a7a0baa01ffe43d9bce1849cadc0e8426f19083351b8.jpg)

![](images/9b8442f2c186cef8f527574b1fb1244613aa87219c600d472348076690325230.jpg)

![](images/40109a50e86248f3043f070378bf98a7dc21d41ad5243decfde80c27bba87595.jpg)

![](images/c24f97de20802b2c7ccfffd988145c2a2c047e18011082bc8f78149a6ac1335f.jpg)

![](images/42f649bc847ee0170d19273dc0242abcdd147d9b5644d17c5cf30a219a7a71a7.jpg)

![](images/42fe4758aade9c313ae1a02151578611ce8f7d2345aec6a8469c30d7820f8050.jpg)

• Example1: Roll two dice.

- Example of an Events  
- $E =$ the two dies come up even and equal $\{(2,2),(4,4),(6,6)\}$  
- $F =$ the sum of the two dice is 8. $\{(2,6),(3,5),(4,4),(5,3),(6,2)\}$ .  
- $E \cup F = \{(2, 2), (2, 6), (3, 5), (4, 4), (5, 3), (6, 2), (6, 6)\}$  
- $E \cap F = \{(4, 4)\}$ .  
- $F^c$ all the 31 other ways that does not include $\{(2,6), (3,5), (4,4), (5,3), (6,2)\}$ .

\- Example2: $S = [0, \infty)$ age someone dies.

\- Event $A =$ person dies before they reached 30.

$$
* A = [ 0, 3 0).
$$

\- Interpret $A^c = [30,\infty)$

\* The person dies after they turned 30.

\- $B = (15, 45)$ . Do $A \cup B, A \cap B$ and so on.

- Properties: Events also have commutative and associate and Distributive laws.  
• What is $A \cup A^{c}$ ? = S.  
- DeMorgan's Law:

- $(A\cup B)^{c} = A^{c}\cap B^{c}$ . Try to draw a picture  
$-\left(A\cap B\right)^{c}=A^{c}\cup B^{c}.$  
- This works for general $A_{1},\ldots ,A_{n}$ : $(\cup_{i = 1}^{n}A_{i})^{c} = \cap_{i = 1}^{n}A_{i}^{c}$ and $(\cap_{i = 1}^{n}A_{i})^{c} = \cup_{i = 1}^{n}A_{i}^{c}$ .

\- The empty set $\emptyset = \{\}$ is the set that has nothing in it.

\- $A$ and $B$ are disjoint if $A \cap B = \emptyset$ .

- In Probability we may say that events $A$ and $B$ are "mututally exclusive" if they are disjoint.  
- mutually exclusive means the same thing as disjoint

## 2.2. Axioms of Probability

\- Let $E$ be an event. How do we defined the probability of an event?

- We can attempt to define a probability by the relative frequency,  
- Perform an experiment (e.g. Flipping a coin)  
- Perform that experiment $n$ times and let $n(E) =$ the number of times the event occurred in $n$ repetitions

\* (e.g. Flip a coin $n = 1000$ times, and let's say that $n(\{Tails\}) = 551$ ) Then it's reasonable to think $\mathbb{P}(\{Tails\}) \approx \frac{551}{1000}$

- So maybe we can define the probability of an event as $\mathbb{P}(E) = \lim_{n\to \infty}\frac{n(E)}{n}$ . But we don't know if this limit exists, or if $n(E)$ is even well defined!!!  
- So we need a new approach.

\- Probability will be a rule given by the following Axioms (Laws that we all agree on)

- A probability will be a function $\mathbb{P}(E)$ where the input is a set/event such that  
- Axiom 1: $0 \leq \mathbb{P}(E) \leq 1$ for all events $E$ .  
- Axiom 2: $\mathbb{P}(S) = 1$ .  
- $\underline{\mathbf{Axiom 3}}$ : (disjoint property) If the events $E_1, E_2, \ldots$ are pairwise disjoint/mutually exclusive then

$$
\mathbb {P} \left(\bigcup_ {i = 1} ^ {\infty} E _ {i}\right) = \sum_ {i = 1} ^ {\infty} \mathbb {P} \left(E _ {i}\right).
$$

\* Mutually exclusive means that $E_{i} \cap E_{j} = \emptyset$ when $i \neq j$ .

\- Remark: Note that you take a probability of a subset of $S$ , not of points of $S$ . However it is common to write $P(x)$ for $P(\{x\})$ .

\- Say if the experiment is tossing a xoin. Then $S = \{H, T\}$ . The probability of heads should be written as $\mathbb{P}(\{H\})$ , but it is common to see $\mathbb{P}(H)$ .

\- Example1:

$-$ (a) Suppose we toss a coin and they are equally likely then $S = \{H,T\}$ and

\* $\mathbb{P}\left(\{H\}\right) = \mathbb{P}\left(\{T\}\right) = \frac{1}{2}$ . We may write $\mathbb{P}(H) = \mathbb{P}(T) = \frac{1}{2}$ .

\- (b) If biased coin is tosse then one could have a different assignment of probability $\mathbb{P}(H) = \frac{2}{3}, \mathbb{P}(T) = \frac{1}{3}$ .

\- Example2:

- Rolling a fair die, the probability space consists of $S = 1,2,3,4,5,6$ , each point having probability $\frac{1}{6}$ .  
- We can compute the probability of rolling an even number by

$$
\mathbb {P} (\{\text { even } \}) = \mathbb {P} (\{2, 4, 6 \})
$$

$$
= \mathbb {P} (2) + \mathbb {P} (4) + \mathbb {P} (6) = \frac {1}{2}
$$

where we used the rules of probability by breaking it down into a sum.

PROPOSITION 1. (a) $\mathbb{P}(\emptyset) = 0$

(b) If $A_{1},\ldots ,A_{n}$ are pairwise disjoint, $\mathbb{P}\left(\cup_{i = 1}^{n}A_{i}\right) = \sum_{i = 1}^{n}\mathbb{P}\left(A_{i}\right)$ .  
(c) $\mathbb{P}(E^c) = 1 - \mathbb{P}(E)$ .  
(d) If $E \subset F$ , then $\mathbb{P}(E) \leq \mathbb{P}(F)$ .  
(e) $\mathbb{P}(E\cup F) = \mathbb{P}(E) + \mathbb{P}(F) - \mathbb{P}(E\cap F)$ .

\- It helps to draw diagrams to prove these.

\- Try to prove at least some of these yourself.

PROOF. (a) Let $A_{i} = \emptyset$ for each $i$ which are disjoint. So

$$
\mathbb {P} (\emptyset) = \mathbb {P} \left(\bigcup_ {i = 1} ^ {\infty} A _ {i}\right) = \sum_ {i = 1} ^ {\infty} \mathbb {P} (A _ {i}) = \sum_ {i = 1} ^ {\infty} \mathbb {P} (\emptyset),
$$

since this would be infinite sum so that $\mathbb{P}(\emptyset) = 0$ since $0 \leq \mathbb{P}(\emptyset) \leq 1$ .

(b) Let $A_{n+1} = A_{n+2} = \cdots = \emptyset$ so that $\cup_{i=1}^{\infty} A_{i} = \cup_{i=1}^{n} A_{i}$ hence

$$
\begin{array}{l} \mathbb {P} \left(\cup_ {i = 1} ^ {n} A _ {i}\right) = \mathbb {P} \left(\cup_ {i = 1} ^ {\infty} A _ {i}\right) \\ = \sum_ {i = 1} ^ {n} \mathbb {P} \left(A _ {i}\right) + \sum_ {n = 1} ^ {\infty} \mathbb {P} (\emptyset) \\ = = \sum_ {i = 1} ^ {n} \mathbb {P} \left(A _ {i}\right) + \sum_ {n = 1} ^ {\infty} 0 \\ = \sum_ {i = 1} ^ {n} \mathbb {P} \left(A _ {i}\right) \\ \end{array}
$$

(c) Use $S = E \cup E^{c}$ . By Axiom (2) we have

$$
1 = \mathbb {P} (S) = \mathbb {P} (E) + \mathbb {P} (E ^ {c}),
$$

hence $\mathbb{P}(E^c) = 1 - \mathbb{P}(E)$ .

(d) If $E \subset F$ , then write $F = E \cup (F \cap E^c)$ thus since this is disjoint

$$
\mathbb {P} (F) = \mathbb {P} (E \cup (F \cap E ^ {c})) = \mathbb {P} (E) + \mathbb {P} (F \cap E ^ {c}) \geq \mathbb {P} (E) + 0 = \mathbb {P} (E).
$$

(e) Write $E \cup F = E \cup (E^c \cap F)$ , (Picture of venn diagram of both ) hence by disjointness again

$$
\mathbb {P} (E \cup F) = \mathbb {P} (E) + \mathbb {P} (E ^ {c} \cap F).
$$

Now write $F$ (with picture) as $F = (E \cap F) \cup (E^c \cap F)$ and using disjointness

$$
\mathbb {P} (F) = \mathbb {P} (E \cap F) + \mathbb {P} (E ^ {c} \cap F) \implies \mathbb {P} (E ^ {c} \cap F) = \mathbb {P} (F) - \mathbb {P} (E \cap F),
$$

substitute into first equation to get

$$
\begin{array}{l} \mathbb {P} (E \cup F) = \mathbb {P} (E) + \mathbb {P} (E ^ {c} \cap F) \\ = \mathbb {P} (E) + \mathbb {P} (F) - \mathbb {P} (E \cap F), \\ \end{array}
$$

as needed.

• Example: Uconn Basketball is playing Kentucky this year.

- Home game has .5 chance of winning  
- Away game has .4 chance of winning.  
- .3 that uconn wins both games.  
- What's the probability that Uconn loses both games?  
- Answer.

\* Let $\mathbb{P}(A_1) = .5$ , $\mathbb{P}(A_2) = .4$ and $\mathbb{P}(A_1 \cap A_2) = .3$ .  
\* We want to find $\mathbb{P}(A_1^c\cap A_2^c)$ . Simplify as much as we can:

$$
\begin{array}{l} \mathbb {P} \left(A _ {1} ^ {c} \cap A _ {2} ^ {c}\right) = \mathbb {P} \left(\left(A _ {1} \cup A _ {2}\right) ^ {c}\right) \text { by   DeMorgan's   Law } \\ = 1 - \mathbb {P} \left(A _ {1} \cup A _ {2}\right), \text {   by   Proposition   1c } \\ \end{array}
$$

\* Using Proposition 1e, we have

$$
\mathbb {P} \left(A _ {1} \cup A _ {2}\right) = . 5 +. 4 -. 3 = . 6,
$$

Hence $\mathbb{P}(A_1^c\cap A_2^c) = 1 - .6 = .4$ as needed.

## 2.3. Equally Likely Outcomes

\- In many experiments, a probability space consists of finitely many points, all with equally likely probabilities.

- Basic example was a tossing a coin $P(H) = P(T) = \frac{1}{2}$  
- Fair die: $P(i) = \frac{1}{6}$ for $i = 1, \ldots, 6$ .

\- In this case from Axiom 3 we have that

$$
P (E) = \frac {\text { number   of   outcomes   in } E}{\text { number   of   outcomes   in } S}.
$$

\- Example1: What is the probability that if we roll 2 dice, the sum is 7?

\- Answer: There are 36 total outcomes, of which 6 have a sum of 7:

\* $E =$ "sum is 7" $= \{(1,6),(2,5),(3,4),(4,3),(5,2),(6,1)\}$ . Since they are all equally likely, the probability is $\mathbb{P}(E) = \frac{6}{6\cdot6} = \frac{1}{6}$ .

\- Example 2: If 3 balls are “randomly drawn” from a bowl containing 6 white and 5 black balls, what is the probability that one ball is white and the other two are black?

\- Method 1: (regard as a ordered selection)

$$
\begin{array}{l} P (E) = \frac {W B B + B W B + B B W}{1 1 \cdot 1 0 \cdot 9} \\ = \frac {6 \cdot 5 \cdot 4 + 5 \cdot 6 \cdot 4 + 5 \cdot 4 \cdot 6}{9 9 0} = \frac {1 2 0 + 1 2 0 + 1 2 0}{9 9 0} = \frac {4}{1 1}. \\ \end{array}
$$

\- Method2: (Regard as unordered set of drawn balls)

$$
P \left(E\right) = \frac {\left(1 \text {white}\right) \left(2 \text {black}\right)}{\binom{1 1}{3}} = \frac {\binom{6}{1} \binom{5}{2}}{\binom{1 1}{3}} = \frac {4}{1 1}.
$$

\- We can always choose which way to regard our experiments.

\- Example 3 A committee of 5 is to selected from a group of 6 men and 9 women. What is probability consists of 3 men and 2 women

\- Answer: Easy $\frac{men\cdot women}{all}=\frac{\left(\begin{array}{c}6\\3\end{array}\right)\left(\begin{array}{c}9\\2\end{array}\right)}{\left(\begin{array}{c}15\\5\end{array}\right)}=\frac{240}{1001}.$

\- Example 4: Seven balls are randomly withdrawn from an urn that contains 12 red, 16 blue, and 18 green.

- (b) Find probability that “at least 2 red balls are withdrawn;”  
- $\underline{\mathbf{Ans}}$ : Let $E$ be this event then $P(E) = 1 - P(E^c)$ , $P$ (at least 2 red) = 1- $\mathbb{P}$ (drawing 0 or 1 balls).

$$
\mathbb {P} \left(\text { drawing   0   or   1   red   balls }\right) = \frac {\binom{1 6 + 1 8 = 3 4}{7}}{\binom{4 6}{7}} + \frac {\binom{1 2}{1} \binom{3 4}{6}}{\binom{4 6}{7}}.
$$

\- Explanation of Poker/Playing cards: Ranks and suits,etc!

\- There are 52 cards in a standard deck of playing cards. The poker hand is consists of five cards. There are 4 suits: heats, spades, diamonds, and clubs ( $\heartsuit$ ♠♦♣). The suits diamonds and hearts are red while clubs and spades are black. In each suit there are 13 ranks: the numbers 2, 3, ..., 10, the face cards, Jack, Queen, King, and the Ace (not a face card).

\- Example 5: What is the probability that in a poker hand (5 cards out of 52) we get exactly 4 of a kind?

\- Answer: Consider 4 aces and 1 king: $AAAK = \left( \begin{array}{l}4\\ 4 \end{array} \right)\left( \begin{array}{l}4\\ 1 \end{array} \right)$ . But $JJJJ3$ is the same probability.

\* Thus there are 13 ways to pick the first rank, and 12 ways to pick the second rank

$$
\begin{array}{l} P (4 \text {   of   a   kind   }) = \frac {[ \text {   choice   of   ranks   } ] [ \text {   given   rank   how   to   choose   a   hand   } ]}{\binom{5 2}{5}} \\ = \frac {\left(1 3 \cdot 1 2 \cdot \binom{4}{4} \binom{4}{1}\right)}{\binom{5 2}{5}} \approx . 0 0 0 0 1 3 9 \\ \end{array}
$$

\- Example 6: What is the probability that in a poker hand (5 cards out of 52) we get a straight. (no straight flushes, can't be of the same suit)

\- Answer: Consider: A-2-3-4-5-6-7-8-9-10-J-Q-K-A- There are 10 possible straights.

\* Given a straight Say A2345: There are $4 \cdot 4 \cdot 4 \cdot 4 \cdot 4 - (\text{of the same suit}) = 4^5 - 4$ .

$$
\begin{array}{l} P (\text { Straight }) = \frac {[ \text { choice   of   straight } ] [ \text { given   striaght   how   to   choose   a   hand } ]}{\binom{5 2}{5}} \\ = \frac {1 0 \cdot (4 ^ {5} - 4)}{\binom{5 2}{5}} \approx . 0 0 3 9 \\ \end{array}
$$

\- Example 7: What is the probability that in a poker hand (5 cards out of 52) we get a Full House. (3 and a 2 of a kind)

- Answer: It would be [3 of a kind][2 of a kind]. AAAKK or KKAAA are different! Choose suit: $13 \cdot 12$ .  
- Then once we choose within each group there

$$
P (\text {Full House}) = \frac {[ \text {choice of rank} ] [ 3 \text {of a kind} ] [ 2 \text {of a kind} ]}{\binom{5 2}{5}}
$$

$$
= \frac {1 3 \cdot 1 2 \binom{4}{3} \binom{4}{2}}{\binom{5 2}{5}} \approx . 0 0 1 4.
$$

\- Example 8: (Birthday Problem) In a class of 32 people, what is the probability that at least two people have the same birthdays? (We assume each day is equally likely.)

\- Answer: Let the first person have a birthday on some day. The probability that the second person has a different birthday will be $\frac{364}{365}$ . The probability that the third person has a different birthday from the first two people is $\frac{363}{365}$ . So the answer is

$$
\mathbb {P} \left(\text {at least 2 people}\right) = 1 - \mathbb {P} \left(\text {Everyone different birthday}\right)
$$

$$
= 1 - \frac {3 6 5}{3 6 5} \cdot \frac {3 6 4}{3 6 5} \cdot \frac {3 6 3}{3 6 5} \dots \frac {(3 6 5 - 3 1)}{3 6 5}
$$

$$
= 1 - 1 \cdot \frac {3 6 4}{3 6 5} \cdot \frac {3 6 3}{3 6 5} \dots \frac {3 3 4}{3 6 5} \approx 0. 7 5 2 3 7 4.
$$

\- Really High!!!

## CHAPTER 3

## Independence

## 3.1. Independent Events

DEFINITION. We say $E$ and $F$ are independent events if

$$
\mathbb {P} (E \cap F) = \mathbb {P} (E) \mathbb {P} (F).
$$

• Example1: Suppose you flip two coins.

- The event that you get heads on the second coin is independent of the event that you get tails on the first.  
- This is why: Let $A_{t}$ be the event of getting is tails for the first coin and $B_{h}$ is the event of getting heads for the second coin, and we assume we have fair coins (although this is not necessary), then

$$
\mathbb {P} \left(A _ {t} \cap B _ {h}\right) = \frac {1}{4}, \text {   list   out   all   outcomes }
$$

$$
\mathbb {P} \left(A _ {t}\right) \mathbb {P} \left(B _ {h}\right) = \frac {1}{2} \frac {1}{2} = \frac {1}{4}.
$$

\- Example2: Experiment: Draw a card from an ordinary deck of cards

\- Let $A =$ draw ace, $S =$ draw a spade.

\* These are independent events since you're taking one at a time, so one doesn't effect the other. To see this using the definition we have compute  
\* $\mathbb{P}(A)\mathbb{P}(S) = \frac{1}{13}\frac{1}{4}.$  
\* White $\mathbb{P}(A\cap S) = \frac{1}{52}$ since there is only 1 Ace of spades.

PROPOSITION 2. If $E$ and $F$ are independent, then $E$ and $F^c$ are independent.

PROOF. Draw a Venn Diagram to help with the computation, but note that

$$
\begin{array}{l} \mathbb {P} (E \cap F ^ {c}) = \mathbb {P} (E) - \mathbb {P} (E \cap F) \\ = \mathbb {P} (E) - \mathbb {P} (E) \mathbb {P} (F) \\ = \mathbb {P} (E) (1 - \mathbb {P} (F)) \\ = \mathbb {P} (E) \mathbb {P} \left(F ^ {c}\right). \\ \end{array}
$$

![](images/2dce10c023707461abdcde66f4a4f1023cba8c47f7e93817c621289c12bd2eeb.jpg)

\- Remark: Independence and mutually exclusive, are two different things!

DEFINITION. We say $E, F, G$ are independent if $E, F$ are independent, $E, G$ are independent, $F, G$ are independent, and $\mathbb{P}(E \cap F \cap G) = \mathbb{P}(E)\mathbb{P}(F)\mathbb{P}(G)$ .

• Example: Experiment is you roll two dice:

\- Define the following events:

- $S_{7} = \{\text{sum is 7}\}$  
- $A_{4} = \{\text{first die is a 4}\}$  
- $B_{3} = \{$ second die is a 3\}  
- Are the events $S_{7}, A_{4}, B_{3}$ independent?

\* Compute

$$
\mathbb {P} \left(S _ {7} \cap A _ {4} \cap B _ {3}\right) = \mathbb {P} \left(\{(4, 3) \}\right) = \frac {1}{3 6}
$$

but

$$
\mathbb {P} \left(S _ {7}\right) \mathbb {P} \left(A _ {4}\right) \mathbb {P} \left(B _ {3}\right) = \frac {6}{3 6} \frac {1}{6} \frac {1}{6} = \frac {1}{3 6 \cdot 6}.
$$

\- Remark: This generalizes to events $A_1, \ldots, A_n$ . We say events $A_1, \ldots, A_n$ are independent if for all subcollections $i_1, \ldots, i_r \in \{1, \ldots, n\}$ we have that $\mathbb{P}\left(\bigcap_{j=1}^{r} A_{i_j}\right) = \prod_{j=1}^{r} \mathbb{P}\left(A_{i_j}\right)$ .

• Example:

- An urn contains 10 balls: 4 red and 6 blue.  
- A second urn contains 16 red balls and an unknown number of blue balls.  
- A single ball is drawn from each urn. The probability that both balls are the same color is 0.44.  
- Question: Calculate the number of blue balls in the second urn.  
- Solution: Let $R_{i} =$ even that a red ball is drawn from urn $i$ and let $B_{i} =$ event that a blue ball is drawn from urn $i$ .

\* Let $x$ be the number of blue balls in urn 2,  
\* Note that drawing from urn 1 and independent from drawing from urn 2. They are completely different urns! They shouldn't effect the other.  
\* Then

$$
\begin{array}{l} . 4 4 = \mathbb {P} \left(\left(R _ {1} \cap R _ {2}\right) \bigcup \left(B _ {1} \cap B _ {2}\right)\right) = \mathbb {P} \left(R _ {1} \cap R _ {2}\right) + \mathbb {P} \left(B _ {1} \cap B _ {2}\right) \\ = \mathbb {P} (R _ {1}) \mathbb {P} (R _ {2}) + \mathbb {P} (B _ {1}) \mathbb {P} (B _ {2}), \text {   by   independence } \\ = \frac {4}{1 0} \frac {1 6}{x + 1 6} + \frac {6}{1 0} \frac {x}{x + 1 6}. \\ \end{array}
$$

\* Solve for $x!$ You will get $x = 4$ .

\- Example (Gambler's Ruin) (Used in Finance or Actuarial Science)

\- Experiment: Suppose you toss a fair coin repeatedly and independently. If it comes up heads, you win a dollar, and if it comes up tails, you lose a dollar. Suppose you start with \$50. What's the probability you will get to \$200 before you go broke?

\- Answer: It's actually easier if we generalize the problem.

\* Let $p(x)$ be the probability you get 200 before 0 if you start with $x$ dollars.  
\* We know $p(0) = 0$ and $p(200) = 1$ . So by the law of total probability

$$
\begin{array}{l} p (x) = \mathbb {P} (\text { Win   200   before   0 }) \\ = \mathbb {P} (H) \mathbb {P} (\text { Win } 2 0 0 \text { before } 0 \mid H) + \mathbb {P} \left(H ^ {c}\right) \mathbb {P} (\text { Win } 2 0 0 \text { before } 0 \mid H ^ {c}) \\ = \frac {1}{2} p (x + 1) + \frac {1}{2} p (x - 1). \\ \end{array}
$$

\* Rearrange the function to get

$$
\begin{array}{l} 2 p (x) = p (x - 1) + p (x + 1) \quad \Longleftrightarrow \quad p (x) + p (x) = p (x - 1) + p (x + 1) \\ \Longleftrightarrow \quad p (x) - p (x - 1) = p (x + 1) - p (x) \\ \Longleftrightarrow \quad \frac {p (x) - p (x - 1)}{x - (x - 1)} = \frac {p (x + 1) - p (x)}{(x + 1) - x}. \\ \end{array}
$$

\* This tells you that the slows are constant. What does that tell you about $p(x)$ ? It's a line!

\- Thus we must have $p(x) = \frac{x}{200}$ .

\* Thus $p(50) = \frac{1}{4}$ .

• Example (A variation of Gambler's ruin)

\- Problem: Suppose we are in the same situation, but you are allowed to go arbitrarily far in debt. Let $p(x)$ be the probability you ever get to \$200. What is a formula for $p(x)$ ?

\* Answer: Just as before $p(x) = \frac{1}{2} p(x + 1) + \frac{1}{2} p(x - 1)$ . So that $p(x)$ is linear.

\* But now all we have is that $p(200) = 1$ and linear and domain is $(-\infty, 200)$ .

\* Draw a graph: Now the slope, or $p'(x)$ can't be negative, or else we would have it that $p(x) > 1$ for $x \in (-\infty, 200)$ .

\- The slope can't be positive or else we would get $p(x) < 0$ for $x \in (-\infty, 200)$ .

\* Thus we must have that $p(x) \equiv \text{constant}$ . Hence $p(x) = 1$ for all $x \in (-\infty.200)$ .

\* Sol: So we are certain to get \$200 if we cna get into debt.

\- Method2:

\* Just compute There is nothing special about the figure 200. Another way of seeing this is to compute as above the probability of getting to 200 before $-M$ and then letting $M \to \infty$ .

\- We would get $p(x)$ is a line with $p(-M) = 0$ and $p(200) = 1$ so that

$$
p (x) - 0 = \frac {1 - 0}{2 0 0 - (- M)} (x - (- M))
$$

and letting $M \to \infty$ wee see that $p(x) = \frac{x + M}{200 + M} \to 1$ .

• Example: Experiment: Roll 10 dice.

- What is the probability that exactly 4 twos will show if you roll 10 dice?  
- Answer: These are independent. The probability that the 1st, 2nd, 3rd, and 10th dice will show a three and the other 6 will not is $\left(\frac{1}{6}\right)^3\left(\frac{5}{6}\right)^7$ .  
- Independence is used here: the probability is $\frac{1}{6}\frac{1}{6}\frac{1}{6}\frac{5}{6}\frac{5}{6}\frac{5}{6}\frac{5}{6}\frac{5}{6}\frac{5}{6}\frac{1}{6}$ . Note that the probability that the 10th, 9th, 8th, and 7th dice will show a two and the other 6 will not have the same probability.  
- So to answer our original question, we take $\left(\frac{1}{6}\right)^4\left(\frac{5}{6}\right)^6$ and multiply it by the number of ways of choosing 4 dice out of 10 to be the ones showing the twos. There are $\binom{10}{3}$ ways to do this $\binom{10}{4}\left(\frac{1}{6}\right)^4\left(\frac{5}{6}\right)^6$ .

\- This is an example of Bernoulli trials, or the Binomial distribution.

\- If we have $n$ independent trials, where the probability of success if $p$ . The probability that there are $k$ successes in $n$ trials is

$$
\binom{n}{k} p ^ {k} (1 - p) ^ {n - k}.
$$

## CHAPTER 4

## Conditional Probability and Independence

## 4.1. Conditional Probabilities

\- Suppose there are

- 200 men, of which 100 are smokers,  
- 100 women, of which 20 are smokers.  
- Question1: What is the probability that a person chosen at random will be a smoker? $\frac{120}{300}$  
- Question2: Now, let us ask, what is the probability that a person chosen at random is a smoker given that the person is a women? $\frac{20}{100}$ right?

\* Note this is

$$
\frac {\# (\text { women   smokers })}{\# (\text { women })} = \frac {P (\text { women   and   a   smoker })}{P (\text { woman })}.
$$

\- Thus we make the following definition:

DEFINITION. If $\mathbb{P}(F) > 0$ , we define

$$
\mathbb {P} (E \mid F) = \frac {\mathbb {P} (E \cap F)}{\mathbb {P} (F)}.
$$

Now $\mathbb{P}(E\mid F)$ is read "the probability of $E$ given $F$ ."

- Note that $\mathbb{P}(E\cap F) = \mathbb{P}(E|F)\mathbb{P}(F)!$  
- This is the conditional probability that $E$ occurs given that $F$ has already occurred!  
- Remark: Suppose $\mathbb{P}(E \mid F) = \mathbb{P}(E)$ , i.e. knowing $F$ doesn't help predict $E$ . Then this implies that $E$ and $F$ are independent of each other. Rearranging $\mathbb{P}(E \mid F) = \frac{\mathbb{P}(E \cap F)}{\mathbb{P}(F)} = \mathbb{P}(E)$ we see that $\mathbb{P}(E \cap F) = \mathbb{P}(E)\mathbb{P}(F)$ .  
- Example1: Experiment: Roll two dice.  
- (a) What is the probability the sum is 8?  
\* Solution: Note that $A = \{(2,6), (3,5), (4,4), (5,3), (6,2)\}$ so we know $\mathbb{P}(A) = \frac{5}{36}$ .  
- (b) What is the probability that the sum is 8 given that the first die shows a 3? (In other words, find $\mathbb{P}(A \mid B)$ )  
\* Solution: Let $B = \{\text{first die shows three}\}$ .  
\* $\mathbb{P}(A\cap B) = \mathbb{P}\left(\{(3,5)\}\right) = \frac{1}{36}$ is probability that the first die shows a 3 and the sum is 8  
\* Finally we can compute

$$
\mathbb {P} (A \mid B) = \mathbb {P} (\text { sum   is } 8 \mid 1 \text { st   is   a } 3) = \frac {1 / 3 6}{1 / 6} = \frac {1}{6}.
$$

\- Remark: When computing $\mathbb{P}(E \mid F)$ , Sometime its easier to work with the reduced sample space $F \subset S$ .

\- Note in the previous example when we computed

$$
\mathbb {P} (\text { sum   is } 8 \mid 1 \text { st   is   a } 3)
$$

we could have worked in the smaller sample space of $\{1st\ is\ a\ 3\}=\{(3,1),(3,2),(3,3),(3,4),(3,5),(3,6)\}$ . Since only $(3,5)$ begins with a 3 and has the sum of 8, then the probability is

$$
\frac {\text { total   number   of   outcomes   in   the   event }}{\text { total   number   of   outcomes   in   new   sample   space }} = \frac {1}{6}.
$$

\- Example2: Experiment: Suppose a box has 3 red marbles and 2 black ones. We select 2 marbles.
  - Question: What is the probability that second marble is red given that the first one is red?

\* Answer:

$\overline{\cdot R_1} = \{\text{First marble is is red}\}$ ,

$R_{2} = \{\text{Second marble is red}\}$ , then

$$
\begin{array}{l} \mathbb {P} \left(R _ {2} \mid R _ {1}\right) = \frac {\mathbb {P} \left(R _ {1} \cap R _ {2}\right)}{\mathbb {P} \left(R _ {1}\right)} \\ = \frac {(2 \text {   red }) (0 \text {   black }) / \binom{5}{2}}{3 / 5} \\ = \frac {\binom{3}{2} \binom{2}{0} / \binom{5}{2}}{3 / 5} \\ = \frac {3 / 1 0}{3 / 5} = \frac {1}{2}. \\ \end{array}
$$

\* Solution 2:

\- We could have done the same example more easily if we look at the new sample space $S' = \{R, R, B, B\}$ thus $\mathbb{P}(R_2 \mid R_1) = \mathbb{P}'(\{\text{drawing red}\}) = \frac{2}{4} = \frac{1}{2}$ .

\- Example3: Landon is 80% sure he forgot his textbook at the Union or Monteith buildings. 40% sure that it is at the union, and 40% sure that it is at Monteith. Given that Landon already went to Monteith and noticed his textbook not there, what is the probability that it's at the Union?

\- Solution:

$$
\begin{array}{l} \mathbb {P} \left(U n i o n \mid N o t \text {   Monteith }\right) = \frac {\mathbb {P} \left(U \cap M ^ {c}\right)}{\mathbb {P} \left(M ^ {c}\right)} \\ = \frac {\mathbb {P} (U)}{1 - \mathbb {P} (M)}, \text { since } U \subset M ^ {c} \\ = \frac {4 / 1 0}{6 / 1 0} = \frac {2}{3}. \\ \end{array}
$$

\- Example4: Suppose that Annabelle and Bobby each draw 13 cards from a standard deck of 52. Given that Sarah has exactly two aces, what is the probability that Bobby has exactly one ace?

\- Solution: Let $A$ be the event "Annabelle has two aces," and let $B$ be the event "Bobby has exactly one ace." Again, we want $\mathbb{P}(B \mid A)$ , so we calculate $\mathbb{P}(A)$ and $\mathbb{P}(A \cap B)$ . Annabelle could have any of $\left( \begin{array}{c}52\\ 13 \end{array} \right)$ possible hands. Of these hands, $\left( \begin{array}{c}4\\ 2 \end{array} \right) \cdot \left( \begin{array}{c}48\\ 11 \end{array} \right)$ will have exactly

two aces, so

$$
\mathbb {P} \left(A\right) = \frac {\binom{4}{2} \cdot \binom{4 8}{1 1}}{\binom{5 2}{1 3}}.
$$

Now the number of ways in which Annabelle can have a certain hand and Bobby can have a certain hand is $\binom{52}{13}\cdot\binom{39}{13}$ , and the number of ways in which A and B can both occur is $\binom{4}{2}\cdot\binom{48}{11}\cdot\binom{2}{1}\cdot\binom{37}{12}$ . so

$$
\mathbb {P} (A \cap B) = \frac {\binom{4}{2} \cdot \binom{4 8}{1 1} \cdot \binom{2}{1} \cdot \binom{3 7}{1 2}}{\binom{5 2}{1 3} \cdot \binom{3 9}{1 3}}.
$$

Therefore,

$$
\begin{array}{l} \mathbb {P} (B \mid A) = \frac {\mathbb {P} (A \cap B)}{\mathbb {P} (A)} = \frac {\frac {\left( \begin{array}{c} 4 \\ 2 \end{array} \right) \cdot \left( \begin{array}{c} 4 8 \\ 1 1 \end{array} \right) \cdot \left( \begin{array}{c} 2 \\ 1 \end{array} \right) \cdot \left( \begin{array}{c} 3 7 \\ 1 2 \end{array} \right)}{\left( \begin{array}{c} 5 2 \\ 1 3 \end{array} \right) \cdot \left( \begin{array}{c} 3 9 \\ 1 3 \end{array} \right)}}{\frac {\left( \begin{array}{c} 4 \\ 2 \end{array} \right) \cdot \left( \begin{array}{c} 4 8 \\ 1 1 \end{array} \right)}{\left( \begin{array}{c} 5 2 \\ 1 3 \end{array} \right)}} \\ = \frac {\binom{2}{1} \cdot \binom{3 7}{1 2}}{\binom{3 9}{1 3}}. \\ \end{array}
$$

\- Note that since $\mathbb{P}(B \mid A) = \frac{\mathbb{P}(A \cap B)}{\mathbb{P}(A)}$ then $\mathbb{P}(A \cap B) = \mathbb{P}(A)\mathbb{P}(B \mid A)$ .

\- In general: If $E_1, \ldots, E_n$ are events then

$$
\mathbb {P} \left(E _ {1} \cap E _ {2} \cap \dots \cap E _ {n}\right) = \mathbb {P} \left(E _ {1}\right) \mathbb {P} \left(E _ {2} \mid E _ {1}\right) \mathbb {P} \left(E _ {3} \mid E _ {1} \cap E _ {2}\right) \dots \mathbb {P} \left(E _ {n} \mid E _ {1} \cap E _ {2} \cap \dots \cap E _ {n - 1}\right).
$$

\- Example5:

\- Experiment: Suppose an urn has 5 White balls and 7 Black balls. Each ball that is selected is returned to the urn along with an additional ball of the same color. Suppose draw 3 balls.

\- Part (a): What is the probability that you get 3 white balls.

\* Then

$$
\mathbb {P} (3 \text {white balls}) = \mathbb {P} (1 \text {st W}) \mathbb {P} (2 \text {nd W} | 1 \text {st W}) \mathbb {P} (3 \text {nd W} | 1 \text {st} \& 2 \text {ndW})
$$

$$
= \frac {5}{1 2} \frac {6}{1 3} \frac {7}{1 4}
$$

\- Part (b): What is the probability of getting 1 white ball.

$$
\begin{array}{l} \mathbb {P} (1 \text {   white   ball }) = \mathbb {P} (W B B) + \mathbb {P} (B W B) + \mathbb {P} (B B W) \\ = 3 \frac {5 \cdot 7 \cdot 8}{1 2 \cdot 1 3 \cdot 1 4}. \\ \end{array}
$$

\- Note that

$$
\mathbb {P} (E \cap F) = \mathbb {P} (E \mid F) \mathbb {P} (F)
$$

\- Example 6: Phan wants to take a Biology course or a Chemistry course. Given that the students take Biology, the probability that they get an $A$ is is $\frac{4}{5}$ . While the probability of getting an $A$ given that the student took Chemistry is $\frac{1}{7}$ . If Phan makes a decision on the course to take randomly, what's probability of "getting an $A$ in Chem"?

\- Solution: Let $B = \{\text{Takes Biology}\}$ and $C = \{\text{Takes Chemistry}\}$ and $A = \{"\text{gets an A"}\}$ , then

$$
\begin{array}{l} \mathbb {P} (A \cap C) = \mathbb {P} (C) \mathbb {P} (A \mid C) \\ = \frac {1}{2} \cdot \frac {1}{7} = \frac {1}{1 4}. \\ \end{array}
$$

• Example 7: A total of 500 married couples are poled about salaries:

<table><tr><td>Wife</td><td>Husband makes less than 25,000</td><td>Husband makes more than 25,000</td></tr><tr><td>Less than $25,000</td><td>212</td><td>198</td></tr><tr><td>More than $25,000</td><td>36</td><td>54</td></tr></table>

\- Part (a): Find the probability that a Husband earns less than 25,000?

$$
* \underline {{\text {Answer:}}} \frac {2 1 2 + 3 6}{5 0 0}
$$

\- Part (b): Find $\mathbb{P}$ (wife makes $>25,000$ | Husband makes $>25,000$ )

$$
* \underline {{\text {Answer:}}} \frac {5 4 / 5 0 0}{(1 9 8 + 5 4) / 5 0 0} = \frac {5 4}{2 5 2} = . 2 1 4
$$

\- Part (c): Find $\mathbb{P}$ (wife makes $>25,000$ | Husband makes $< 25,000$ )

$$
* \underline {{\text { Answer: }}} \frac {3 6 / 5 0 0}{(2 4 8) / 5 0 0} = . 1 4 5.
$$

## 4.2. Bayes's Formula

- Sometimes it's easier to compute a probability once we know something has or has not happened.  
- Note that we can compute,

$$
\mathbb {P} (E) = \mathbb {P} (E \cap F) + \mathbb {P} (E \cap F ^ {c})
$$

$$
= \mathbb {P} (E \mid F) \mathbb {P} (F) + \mathbb {P} (E \mid F ^ {c}) \mathbb {P} (F ^ {c})
$$

$$
= \mathbb {P} (E \mid F) \mathbb {P} (F) + \mathbb {P} (E \mid F ^ {c}) (1 - \mathbb {P} (F)).
$$

\- This formula is called: The Law of Total Probability:

$$
\mathbb {P} (E) = \mathbb {P} (E \mid F) \mathbb {P} (F) + \mathbb {P} (E \mid F ^ {c}) (1 - \mathbb {P} (F))
$$

\- The following problem will describe the types of problems of this section.

• Example1: Insurance company believes

- The probability that “an accident prone person” has an accident within a year is .4.  
- The probability that “Non-accident prone person” has an accident with year is .2.  
- $30\%$ of the population is "accident prone".  
- Part (a): Find $\mathbb{P}(A_1)$ where $A_{1} =$ new policy holder will have an accident within a year?

\* Let $A = \{\text{Policy holder IS accident prone.}\}$

$$
\mathbb {P} \left(A _ {1}\right) = \mathbb {P} \left(A _ {1} \mid A\right) \mathbb {P} (A) + \mathbb {P} \left(A _ {1} \mid A ^ {c}\right) \left(1 - \mathbb {P} (A)\right)
$$

$$
= . 4 (. 3) +. 2 (1 -. 3)
$$

$$
= . 2 6
$$

\- Part (b): Suppose new policyholder has accident with one year. What's probability that he or she is accident prone?

$$
\begin{array}{l} \mathbb {P} (A \mid A _ {1}) = \frac {\mathbb {P} (A \cap A _ {1})}{\mathbb {P} (A _ {1})} \\ = \frac {\mathbb {P} (A) \mathbb {P} \left(A _ {1} \mid A\right)}{. 2 6} \\ = \frac {(. 3) (. 4)}{. 2 6} = \frac {6}{1 3}. \\ \end{array}
$$

\- In general:

\- So in Part (a) we had to break a probability into two cases: If $F_1, \ldots, F_n$ are mutually exclusive events such that they make up everywhere $S = \bigcup_{i=1}^{n} F_i$ then

$$
\mathbb {P} (E) = \sum_ {i = 1} ^ {n} \mathbb {P} (E \mid F _ {i}) \mathbb {P} (F _ {i}).
$$

\* This is called Law of Total Probability.

\- In Part (b), we wanted to find a probability of a separate conditional event: then

$$
\mathbb {P} \left(F _ {j} \mid E\right) = \frac {\mathbb {P} \left(E \mid F _ {j}\right) \mathbb {P} \left(F _ {j}\right)}{\sum_ {i = 1} ^ {n} \mathbb {P} \left(E \mid F _ {i}\right) \mathbb {P} \left(F _ {i}\right)}.
$$

\* This is known as Baye's Formula  
\* Note that the denominator of the Bayes's formula is the Law of total probability.

• Example2: Suppose the test for HIV is

- $98\%$ accurate in both directions  
- $0.5\%$ of the population is HIV positive.

- Question: If someone tests positive, what is the probability they actually are HIV positive?  
- Solution: Let $T_{+} = \{\text{tests positive}\}$ , $T_{-} = \{\text{tests negative}\}$ , while $+ = \{\text{actually HIV positive},\}$ $- = \{\text{actually negative}\}$ .

\* Want

$$
\begin{array}{l} \mathbb {P} (+ \mid T _ {+}) = \frac {\mathbb {P} (+ \cap T _ {+})}{\mathbb {P} (T _ {+})} \\ = \frac {\mathbb {P} (T _ {+} \mid +) \mathbb {P} (+)}{\mathbb {P} (T _ {+} \mid +) \mathbb {P} (+) + \mathbb {P} (T _ {+} \mid -) \mathbb {P} (-)} \\ = \frac {(. 9 8) (. 0 0 5)}{(. 9 8) (. 0 0 5) + . 0 2 (. 9 9 5)} \\ = 19.8 \% . \\ \end{array}
$$

• Example3: Suppose

- $30\%$ of the women in a class received an A on the test  
- $25\%$ of the men/or else received an A.  
- $60\%$ of the class are women.  
- Question: Given that a person chosen at random received an A, what is the probability this person is a women?

\* Solution: Let A the event that a students receives an A. Let W = being a women, M = not a women. Want

$$
\mathbb {P} (W \mid A) = \frac {\mathbb {P} (A \mid W) \mathbb {P} (W)}{\mathbb {P} (A \mid W) \mathbb {P} (W) + \mathbb {P} (A \mid M) \mathbb {P} (M)}, \text { by   Bayes's }
$$

$$
= \frac {. 3 (. 6)}{. 3 (. 6) + . 2 5 (. 4)} = \frac {. 1 8}{. 2 8} \approx . 6 4.
$$

- (General Baye's Theorem) Here's one with more than 3 possibilities:  
- Example4: Suppose in Factory with Machines I, II, III producing Iphones

- Machines I,II,III produce 2%,1%, and 3% defective iphones, respectively.  
- Out of total production, Machines $I$ makes $35\%$ of all Iphones, $II - 25\%$ , $III - 40\%$ .  
- If one Iphone is selected at random from the factory,  
- Part (a): what is probability that one Iphone selected is defective?

$$
\mathbb {P} (D) = P (I) \mathbb {P} (D \mid I) + P (I I) \mathbb {P} (D \mid I I) + P (I I I) \mathbb {P} (D \mid I I I)
$$

$$
= (. 3 5) (. 0 2) + (. 2 5) (. 0 1) + (. 4) (. 0 3)
$$

$$
= \frac {2 1 5}{1 0 , 0 0 0}.
$$

\- Part (b): What is the conditional prob that if an Iphone is defective, that it was produced by machine III?

$$
\begin{array}{l} \mathbb {P} (I I I \mid D) = \frac {\mathbb {P} (I I I) \mathbb {P} (D \mid I I I)}{\mathbb {P} (D)} \\ = \frac {(. 4) (. 0 3)}{2 1 5 / 1 0 , 0 0 0} = \frac {1 2 0}{2 1 5}. \\ \end{array}
$$

\- Example5: In a Multiple Choice Test, students either knows the answer or randomly guesses the answer to a question.

\- Let $m =$ number of choices in a question.

- Let $p =$ the probability that the students knows the answer to a question.  
- Question: What is the probability that the student actually knew the answer, given that the student answers correctly.  
- Solution:  
- Let $K = \{\text{Knows the answer}\}$ and $C = \{\text{Answer's correctly}\}$ . Then

$$
\mathbb {P} (K \mid C) = \frac {\mathbb {P} (C \mid K) \mathbb {P} (K)}{\mathbb {P} (C \mid K) \mathbb {P} (K) + \mathbb {P} (C \mid K ^ {c}) \mathbb {P} (K ^ {c})}
$$

$$
= \frac {1 \cdot p}{1 \cdot p + \frac {1}{m} (1 - p)} = \frac {m p}{1 + (m - 1) p}.
$$

## CHAPTER 5

## Random Variables

## 5.1. Random Variables

\- When we perform an experiment, we are interested in some function of the outcomes, instead of the actual outcome.

\- We want to attach for each outcome, a numerical value.

\- Definition: A random variable is a function $X: S \to \mathbb{R}$ or write $X: \Omega \to \mathbb{R}$ . (Use capital letters to denote r.v)

\- We can think of $X$ as a numerical value that is random, like as if $X$ is a random number.

• Example: Toss a coin

\- Let $X$ be 1 if heads and $X = 0$ if tails

\- Then $X(H) = 1$ and $X(T) = 0$ .

\- We can do calculus on real numbers but not on $\Omega = S = \{H, T\}$ .

• Example: Roll a die

\- Let $X$ denote the outcome, so $X = 1,2,3,4,5,6$ (its random)

\- That is $X(1) = 1, X(2) = 2, \ldots$ .

• Example: Roll a die, define

$$
Y = \left\{ \begin{array}{l l} 1 & \text {   outomce   } = \text {   odd   } \\ 0 & \text {   outomce   } = \text {   even   } \end{array} \right.
$$

\- Can be thought of as

$$
Y (s) = \left\{ \begin{array}{l l} 1 & s = \text {odd} \\ 0 & s = \text {even} \end{array} \right..
$$

\- A common question we'll have is "What values can $X$ attain?"

\- In other words, what is the range of $X$ ? Since $X: S \to ?$

• Example: Toss a coin 10 times

\- Let $X$ be the number of heads showing

\- What random values can $X$ be? 0, 1, 2, ..., 10.

\- Example: In general in $n$ trials, $X$ is the number of successes

\- Example1: Let $X$ be the amount of liability(damages) a driver incurs in a year.

\- $X:S\to [0,\infty)$ .

• Example2: Toss a coin 3 times

\- Let $X$ be the number of heads that appear, so $X = 0,1,2,3$ .

\- In other words, $X:S\to \{0,1,2,3\}$

\- We may assign probabilities to the different values of the random variable:

$$
\begin{array}{l} \mathbb {P} (X = 0) = \mathbb {P} ((T, T, T)) = \frac {1}{2 ^ {3}} = \frac {1}{8} \\ \mathbb {P} (X = 1) = \mathbb {P} ((T, T, H), (T, H, T), (H, T, T)) = \frac {3}{8} \\ \mathbb {P} (X = 2) = \mathbb {P} ((T, H, H), (H, H, T), (H, T, H)) = \frac {3}{8} \\ \mathbb {P} (X = 3) = \mathbb {P} ((H, H, H)) = \frac {1}{8}. \\ \end{array}
$$

\- Note that since $X$ must take the values of 0 through 3 then

$$
1 = \mathbb {P} \left(\bigcup_ {i = 0} ^ {3} \{X = i \}\right) = \sum_ {i = 0} ^ {3} \mathbb {P} (X = i),
$$

which makes sense from our previous calculation.

## 5.2. Discrete Random Variables

DEFINITION. A random variable that can take on at most countable number of possible values is said to be a discrete r.v.

DEFINITION. For a discrete random variable, we can define the probability mass function (pmf), or the density function of $X$ by $p(x) = \mathbb{P}(X = x)$ . Note that $p: \mathbb{R} \to [0,1]$ .

- Note that $(X = x) = (\omega \in \Omega \mid X(\omega) = x)$ is an abbreviation.  
- Let $X$ assume only the values $x_{1}, x_{2}, x_{3} \ldots$

\- In other words, $X: S \to \{x_1, x_2, \ldots\}$

\- Properties of a pmf $p(x)$ :

\* Note that we must have $0 < p(x_i) \leq 1$ for, $i = 1,2,\ldots$ and $p(x) = 0$ for all other values of $x$ can't attain.  
\* Also must have

$$
\sum_ {i = 1} ^ {\infty} p (x _ {i}) = 1.
$$

- We often draw bar graphs for discrete r.v.  
• Example: If we toss a coin

\- $X = 1$ if we have $H$ and $X = 0$ if we have $T$ .

\- Then draw a BAR graph

$$
p _ {X} (x) = \left\{ \begin{array}{l l} \frac {1}{2} & x = 0 \\ \frac {1}{2} & x = 1, \\ 0 & \text {otherwise} \end{array} \right.
$$

- Oftentimes someone has already found the $pmf$ for you, and you can use to compute probabilities.  
- Example: The $pmf$ of $X$ is given by $p(i) = e^{-\lambda \frac{\lambda^i}{i!}}$ for $i = 0,1,2,\ldots$ where $\lambda$ is a parameter (what is this?) that is any positive number

\- Part (a) What values can the random variable $X$ attain? In other words, what is the range of $X$ ?

\* Sol: By definition we have $\mathbb{P}(X = 0) = p(0) = e^{-\lambda}\frac{\lambda^0}{0!} = e^{-\lambda}$

\- Part (b) Find $\mathbb{P}(X = 0)$

\* Sol: By definition we have $\mathbb{P}(X = 0) = p(0) = e^{-\lambda}\frac{\lambda^0}{0!} = e^{-\lambda}$

\- Part (c) Find $\mathbb{P}(X > 2)$

\* Sol: Note that

$$
\mathbb {P} (X > 2) = 1 - \mathbb {P} (X \leq 2)
$$

$$
= 1 - \mathbb {P} (X = 0) - \mathbb {P} (X = 1) - \mathbb {P} (X = 2)
$$

$$
= 1 - p (0) - p (1) - p (2)
$$

$$
= 1 - e ^ {- \lambda} - \lambda e ^ {- \lambda} - \frac {\lambda^ {2} e ^ {- \lambda}}{2}.
$$

## 5.3. Expected Value

\- One of the most important concepts in probability is that of expectation. If $X$ is a random variable that what is the average value of $X$ , that is what is the expected value of $X$ .

DEFINITION. Let X have a pmf $p(x)$ . We define the expectation, or expected value of X to be

$$
\mathbb {E} [ X ] = \sum_ {x: p (x) > 0} x p (x).
$$

- Notation $\mathbb{E}X$ , or $EX$ .  
- Example1: Let $X(H) = 0$ and $X(T) = 1$ . What is $\mathbb{E}X$ ?

$$
\mathbb {E} X = 0 \cdot p (0) + 1 \cdot p (1)
$$

$$
= 0 \frac {1}{2} + 1 \cdot \frac {1}{2} = \frac {1}{2}.
$$

\- Example2: Let $X$ be the outcome when we roll a fair die. What is $\mathbb{E}X$ ?

$$
\begin{array}{l} \mathbb {E} X = 1 \left(\frac {1}{6}\right) + 2 \left(\frac {1}{6}\right) + \dots + 6 \frac {1}{6} \\ = \frac {1}{6} (1 + 2 + 3 + 4 + 5 + 6) = \frac {2 1}{6} = \frac {7}{2} = 3. 5 \\ \end{array}
$$

\- Note that $X$ can never be 3.5, so expectation is to give you an idea, what an exact.

\- Recall infinite series: If $0 \leq x < 1$ then a geometric series is

$$
\begin{array}{l} \sum_ {n = 0} ^ {\infty} x ^ {n} = 1 + x + x ^ {2} + x ^ {3} + \dots \\ = \frac {1}{1 - x}. \\ \end{array}
$$

\- One thing you can do with series is differentiate them and integrate them: So if

$$
1 + x + x ^ {2} + x ^ {3} + \dots + = \frac {1}{1 - x}
$$

then

$$
0 + 1 + 2 x + 3 x ^ {2} + \dots + = \frac {1}{(1 - x) ^ {2}}
$$

\- Example3: Let $X$ be the number or tornadoes in Connecticut per year. Meaning that the random variable $X$ can be any number $X = 0,1,2,3,\ldots$ . Suppose the state of Connecticut did some analysis and found out that

$$
\mathbb {P} (X = i) = \frac {1}{2 ^ {i + 1}}.
$$

- Question: What is $\mathbb{E}X$ ? That is, what is the expected number of tornadoes per year in Connecticut.  
- Solution: Note that $X$ is infinite, but still countable, hence still discrete.

\- Note that

$$
p (i) = \left\{ \begin{array}{l l} \frac {1}{2} & i = 0, \\ \frac {1}{4} & i = 1, \\ \frac {1}{8} & i = 2, \\ \vdots & \vdots \\ \frac {1}{2 ^ {n + 1}} & i = n. \end{array} \right.
$$

\- We have that

$$
\begin{array}{l} \mathbb {E} X = 0 \cdot p (0) + 1 \cdot p (1) + 2 \cdot p (2) + \dots \\ = 0 \cdot \frac {1}{2} + 1 \frac {1}{2 ^ {2}} + 2 \frac {1}{2 ^ {3}} + 3 \frac {1}{2 ^ {4}} + \dots \\ = \frac {1}{2 ^ {2}} \left(1 + 2 \frac {1}{2} + 3 \frac {1}{2 ^ {2}} + \dots\right) \\ = \frac {1}{4} (1 + 2 x + 3 x ^ {2} + \dots), \text {with} x = \frac {1}{2} \\ = \frac {1}{4} \frac {1}{(1 - x) ^ {2}} = \frac {1}{4 (1 - \frac {1}{2}) ^ {2}} = 1. \\ \end{array}
$$

## 5.4. The C.D.F.

DEFINITION. Define $F: R \to [0,1]$ to be the function

$$
F (x) = \mathbb {P} (X \leq x), \quad \text { for   any } - \infty <   x <   \infty
$$

to be the cumulative distribution function, or the distribution function of X., or CDF of X, or c.d.f

\- Note that when $X$ is discrete,

$$
F (x _ {0}) = \mathbb {P} (X \leq x _ {0}) = \sum_ {x \leq x _ {0}} p (x).
$$

- We sometimes use the notation $F_{X}(x)$ to highlight that $F_{X}$ is the CDF of the random variable $X$ .  
- Example: Suppose $X$ is equals to the number of heads in 3 coin flips. From Section 5.1, we calculated the p.m.f to be.:

$$
p (0) = \mathbb {P} (X = 0) = \frac {1}{8}
$$

$$
p (1) = \mathbb {P} (X = 1) = \frac {3}{8}
$$

$$
p (2) = \mathbb {P} (X = 2) = \frac {3}{8}
$$

$$
p (3) = \mathbb {P} (X = 3) = \frac {1}{8}.
$$

Question: Find the c.d.f of $X$ . Plot the graph of the c.d.f.

\- Solution: Summing up the probabilities up to that value of $x$ we get the following:

$$
F (x) = \left\{ \begin{array}{l l} 0 & - \infty <   x <   0 \\ \frac {1}{8} & 0 \leq x <   1 \\ \frac {4}{8} & 1 \leq x <   2 \\ \frac {7}{8} & 2 \leq x <   3 \\ 1 & 3 \leq x <   \infty \end{array} \right..
$$

\- The graph is given by

![](images/82976a735b008c82fde988fc1ac55be22d046f0fa0a3790f3a35aa022acc2a7e.jpg)

<details>
<summary>line chart</summary>

| x    | y   |
| ---- | --- |
| -0.5 | 0.0 |
| 0.0  | 0.2 |
| 1.0  | 0.5 |
| 2.0  | 0.9 |
| 3.0  | 1.0 |
| 3.5  | 1.0 |
</details>

- Note that this is a step function.  
- This function has jumps, and not continuous everywhere.  
- But it looks like it never decreases.

## • Properties of the CDF:

- 1. $F$ is nondecreasing, that is

$$
* \text {   if   } x <   y \text {   then   } F (x) \leq F (y)
$$

\- 2. $\lim_{x\to \infty}F(x) = 1$

\- 3. $\lim_{x\to -\infty}F(x) = 0$

\- 4. $F$ is right continuous. That are two ways that you can think of right continuity:

\* $\lim_{x\to 1^{+}}F(x_n) = F(x)$ , meaning "the limit from the right equals where the function is defined"  
\* If $x_{n} \downarrow x$ is a decreasing sequence then $\lim_{n \to \infty} F(x_{n}) = F(x)$ .

\- We take these properties as facts, though one would normally have to prove these.

\- The following proposition does not have to be proved in class, and can be highlighted with the following example. But we include it here for completeness.

PROPOSITION 3. Let $F_{X}(x)$ be the CDF for some random variable $X$ . Then the following holds:

(a) For any $a \in \mathbb{R}$ , we have $\mathbb{P}(X < a) = \lim_{x \to a^{-}} F_X(x)$  
(b) For any $a \in \mathbb{R}$ , we have $\mathbb{P}(X = a) = F_X(a) - \lim_{x \to a^-} F_X(x)$

PROOF. For part (a).

We first write

$$
\begin{array}{l} (X <   a) = \bigcup_ {n = 1} ^ {\infty} \left(X \leq a - \frac {1}{n}\right) \\ = (X \leq a - 1) \bigcup \left[ \bigcup_ {n = 1} ^ {\infty} \left(a - \frac {1}{n} <   X \leq a - \frac {1}{n + 1}\right) \right] \\ \end{array}
$$

and since the events $E_{n} = \left(a - \frac{1}{n}\leq X\leq a - \frac{1}{n + 1}\right)$ are disjoint then we can use Axiom 3 so prove that

$$
\begin{array}{l} \mathbb {P} (X <   a) = \mathbb {P} (X \leq a - 1) + \sum_ {n = 1} ^ {\infty} \mathbb {P} \left(a - \frac {1}{n} <   X \leq a - \frac {1}{n + 1}\right) \\ = \mathbb {P} (X \leq a - 1) + \lim _ {k \rightarrow \infty} \sum_ {n = 1} ^ {k} \left[ \mathbb {P} \left(X \leq a - \frac {1}{n + 1}\right) - \mathbb {P} \left(X \leq a - \frac {1}{n}\right)\right] \\ = \mathbb {P} (X \leq a - 1) + \lim _ {k \rightarrow \infty} \left[ \mathbb {P} \left(X \leq a - \frac {1}{k + 1}\right) - \mathbb {P} (X \leq a - 1) \right], \text { by   telescoping } \\ = \lim _ {k \rightarrow \infty} \mathbb {P} \left(X \leq a - \frac {1}{k + 1}\right) + \mathbb {P} (X \leq a - 1) - \mathbb {P} (X \leq a - 1) \\ = \lim _ {n \to \infty} F _ {X} \left(a - \frac {1}{n}\right). \\ \end{array}
$$

Now you can replace the sequence $a_{n} = a - \frac{1}{n}$ with any sequence $a_{n}$ that is increasing towards $a$ , and we get the similar result,

$$
\lim _ {n \to \infty} F _ {X} (a _ {n}) = \mathbb {P} (X <   a),
$$

since this holds for all increasing sequences $a_{n}$ towards $a$ , then we've shown that

$$
\lim _ {x \to a ^ {-}} F _ {X} (x) = \mathbb {P} \left(X <   a\right).
$$

For part (b). We use part (a) and get

$$
\begin{array}{l} \mathbb {P} (X = a) = \mathbb {P} (X \leq a) - \mathbb {P} (X <   a) \\ = F _ {X} (a) - \lim _ {x \to a ^ {-}} F _ {X} (x). \\ \end{array}
$$

• Example: Let X have distribution

$$
F (x) = \left\{ \begin{array}{l l} 0 & x <   0 \\ \frac {x}{2} & 0 \leq x <   1 \\ \frac {2}{3} & 1 \leq x <   2 \\ \frac {1 1}{1 2} & 2 \leq x <   3 \\ 1 & 3 \leq x. \end{array} \right.
$$

Graph this and answer the following:

\- Part (a): Compute $\mathbb{P}(2 < X\leq 4)$ . We have that

$$
\begin{array}{l} \mathbb {P} (2 <   X \leq 4) = \mathbb {P} (X \leq 4) - \mathbb {P} (X \leq 2) \\ = F (4) - F (2) \\ = \frac {1}{1 2}. \\ \end{array}
$$

\- Part (b): Compute $\mathbb{P}(X < 3)$ .  
\* We have that

$$
\begin{array}{l} \mathbb {P} (X <   3) = \lim _ {n \to \infty} \mathbb {P} \left(X \leq 3 - \frac {1}{n}\right) \\ = \lim _ {x \to 3 ^ {-}} F _ {X} (x) \\ = \frac {1 1}{1 2} \\ \end{array}
$$

\- Part (c): Compute $\mathbb{P}(X = 1)$ .  
\* We have that

$$
\begin{array}{l} \mathbb {P} (X = 1) = \mathbb {P} (X \leq 1) - \mathbb {P} (X <   1) \\ = F _ {X} (1) - \lim _ {x \rightarrow 1 ^ {-}} F _ {X} (x) \\ = \frac {2}{3} - \lim _ {x \rightarrow 1} \frac {x}{2} \\ = \frac {2}{3} - \frac {1}{2} = \frac {1}{6}. \\ \end{array}
$$

## 5.5. Expected Value of Sums of Random Variables

\- Recall our current definition of $\mathbb{E}X$

\- List out $X = x_{1}, x_{2}, \ldots$ and let $p(x_{i})$ be the density of $X$

$$
- \text {   Then   } \mathbb {E} X = \sum_ {i = 1} ^ {\infty} x _ {i} p (x _ {i}).
$$

\- We need a new definition that will help the linearity of expectation.

\- Goal: If $Z = X + Y$ then $\mathbb{E}[X + Y] = \mathbb{E}X + \mathbb{E}Y$ .

\- Definition 2: Let $S$ (or $\Omega$ ) be the sample space then define

$$
\mathbb {E} X = \sum_ {\omega \in S} X (\omega) \mathbb {P} \left(\{\omega \}\right).
$$

\- Example: Let $S = \{1,2,3,4,5,6\}$ and $X(1) = X(2) = 1$ and $X(3) = X(4) = 3$ and $\overline{X(5)} = X(6) = 5$

\* Def1: We know $X = 1,3,5$ with $p(1) = p(3) = p(5) = \frac{1}{3}$  
\* Then $E X = 1 \cdot \frac{1}{3} + 3 \frac{1}{3} + 5 \frac{1}{3} = \frac{9}{3} = 3.$  
\* Def2: We list all of $S = \{1, 2, 3, 4, 5, 6\}$ and  
\* Then

$$
\mathbb {E} X = X (1) \mathbb {P} (\{1 \}) + \dots + X (6) \cdot \mathbb {P} (\{6 \})
$$

$$
= 1 \frac {1}{6} + 1 \frac {1}{6} + 3 \frac {1}{6} + 3 \frac {1}{6} + 5 \frac {1}{6} + 5 \frac {1}{6} = 3.
$$

\- Difference

- Def1: We list all the values that $X$ can attain and only care about those. (Range)  
- Def2: List all possible outcomes. (Domain)

PROPOSITION 4. If $X$ is a discrete random variable and $S$ is countable, then the two definitions are equivalent

\- NOTE: No need to prove in lecture. But here for completeness.

PROOF. We start with the first definition. Let $X = x_{1}, x_{2}, \ldots$

$$
\begin{array}{l} \mathbb {E} X = \sum_ {x _ {i}} x _ {i} p (x _ {i}) \\ = \sum_ {x _ {i}} x _ {i} \mathbb {P} (X = x _ {i}) \\ = \sum_ {x _ {i}} x _ {i} \sum_ {\omega \in \{\omega : X (\omega) = x _ {i} \}} \mathbb {P} (\omega) \\ = \sum_ {x _ {i}} \sum_ {\omega \in \{\omega : X (\omega) = x _ {i} \}} x _ {i} \mathbb {P} (\omega) \\ = \sum_ {x _ {i}} \sum_ {\omega \in \{\omega : X (\omega) = x _ {i} \}} X (\omega) \mathbb {P} (\omega) \\ = \sum_ {\omega \in S} X (\omega) \mathbb {P} (\omega), \\ \end{array}
$$

where I used that each $S_{i}=\{\omega:X(\omega)=x_{i}\}$ are mutually exclusive events that union up to S.

![](images/6f437c0ac8008a25805533cace1b47a72e8d8d5117b198b302ef248424b007c1.jpg)

\- Using this definition, we can prove linearity of the expectation.

THEOREM 5. (Linearity) If $X$ and $Y$ are discrete random variables and $a \in \mathbb{R}$ then

(a) $\mathbb{E}[X + Y] = \mathbb{E}X + \mathbb{E}Y.$  
(b) $\mathbb{E}[aX] = a\mathbb{E}X$ .

PROOF. We have that

$$
\begin{array}{l} \mathbb {E} [ X + Y ] = \sum_ {\omega \in S} (X (\omega) + Y (\omega)) \mathbb {P} (\omega) \\ = \sum_ {\omega \in S} (X (\omega) \mathbb {P} (\omega) + Y (\omega) \mathbb {P} (\omega)) \\ = \sum_ {\omega \in S} X (\omega) \mathbb {P} (\omega) + \sum_ {\omega \in S} Y (\omega) \mathbb {P} (\omega) \\ = \mathbb {E} X + \mathbb {E} Y. \\ \end{array}
$$

If $a\in \mathbb{R}$ then

$$
\begin{array}{l} \mathbb {E} [ a X ] = \sum_ {\omega \in S} (a X (\omega)) \mathbb {P} (\omega) \\ = a \sum_ {\omega \in S} X (\omega) \mathbb {P} (\omega) \\ = a \mathbb {E} X. \\ \end{array}
$$

□

\- Generality: Linearity is true for general random variable $X_{1}, X_{2}, \ldots, X_{n}$ .

## 5.6. Expectation of a Function of a Random Variable

\- Let $X$ be a random variable.

\- Can we find the expected value of things like $X^2, e^X, \sin X$ etc?

\- Example1: Let $X$ denote a random variable such that

$$
\mathbb {P} (X = - 1) = . 2,
$$

$$
\mathbb {P} (X = 0) = . 5
$$

$$
\mathbb {P} (X = 1) = . 3
$$

Let $Y = X^{2}$ . Find $\mathbb{E}Y$ .

- Solution: Note that $Y = \left\{0^2, (-1)^2, (1)^2\right\} = \{0, 1\}$ .  
- Note that $p_Y(1) = .2 + .3 = .5$ and $p_Y(0) = .5$ .  
- Thus $\mathbb{E}Y = 0\cdot .5 + 1\cdot .5 = .5.$

\- IMPORTANT:

- Note that $\mathbb{E}X^2 = .5$  
- While $(\mathbb{E}X)^2 = .01$ . Not equal!

\* Since $\mathbb{E}X = .3 - .2 = .1$ . Thus

$$
\mathbb {E} X ^ {2} \neq (\mathbb {E} X) ^ {2}.
$$

\- In general, there is a formula for $g(X)$ where $g$ is function. That use the fact that $g(X)$ will be $g(x)$ for some $x$ such that $X = x$ .

THEOREM 6. If $X$ is a discrete random variable that takes values $X \in \{x_1, x_2, x_3, \ldots\}$ with respective probability mass function $p(x_i)$ , then for any real valued function $g: \mathbb{R} \to \mathbb{R}$ we have that

$$
\mathbb {E} \left[ g (X) \right] = \sum_ {i = 1} ^ {\infty} g \left(x _ {i}\right) p \left(x _ {i}\right).
$$

\- NOTE: No need to prove in lecture. But here for completeness.

PROOF. The random variable $Y = g(X)$ can take on values, say $Y = y_{1}, y_{2}, \ldots$ . But we know that

$$
y _ {j} = g (x _ {i})
$$

and as we see there could be more than one value $x_{i}$ such that $y_{j} = g(x_{i})$ . Thus we will group this sum into this fashion: Using the definition of expectation we have that

$$
\begin{array}{l} \mathbb {E} [ Y ] = \sum_ {j} y _ {j} \mathbb {P} (Y = y _ {j}) \\ = \sum_ {j} y _ {j} \mathbb {P} (g (X) = y _ {j}) \\ = (\star). \\ \end{array}
$$

Now

$$
\mathbb {P} (g (X) = y _ {j}) = \mathbb {P} \left(\bigcup_ {i: g (x _ {i}) = y _ {j}} (g (x _ {i}) = y _ {j})\right)
$$

$$
= \sum_ {i: g (x _ {i}) = y _ {j}} p (x _ {i}).
$$

Thus plugging this back into $(\star)$ we have that

$$
\begin{array}{l} \mathbb {E} [ Y ] = \sum_ {j} y _ {j} \sum_ {i: g (x _ {i}) = y _ {j}} p (x _ {i}) \\ = \sum_ {j} \sum_ {i: g (x _ {i}) = y _ {j}} y _ {j} p (x _ {i}) \\ = \sum_ {j} \sum_ {i: g (x _ {i}) = y _ {j}} g (x _ {i}) p (x _ {i}) \\ = \sum_ {i = 1} ^ {\infty} g (x _ {i}) p (x _ {i}), \\ \end{array}
$$

as needed.

![](images/e90273795db24c64a57790f3b937347d00a2b759d188e78546fae7464a083998.jpg)

- Remark: $\mathbb{E}X^{2} = \sum x_{i}^{2}p(x_{i})$ .  
- Example1(Revisted): Let $X$ denote a random variable such that

$$
\mathbb {P} (X = - 1) = . 2,
$$

$$
\mathbb {P} (X = 0) = . 5
$$

$$
\mathbb {P} (X = 1) = . 3
$$

Let $Y = X^{2}$ . Find $\mathbb{E}Y$ .

$$
- \underline {{\text { Sol: }}} \text {   We   have   that   } \mathbb {E} X ^ {2} = \sum x _ {i} ^ {2} p (x _ {i}) = (- 1) ^ {2} (. 2) + 0 ^ {2} (. 5) + 1 ^ {2} (. 3) = . 5.
$$

DEFINITION. We call $\mu = \mathbb{E}X$ to be the mean, or the first moment of $X$ .

The quantity $EX^{n}$ for $n \geq 1$ , is called the nth moment of X.

\- From out theorem we know that the $n$ th moments can be calculated a

$$
\mathbb {E} X ^ {n} = \sum_ {x: p (x) > 0} x ^ {n} p (x).
$$

## 5.7. Variance

- The variance of a r.v. is a measure of how spread out the values of $X$ are.  
- The expectation of a r.v. is quantity that help us differentiate different r.v.'s, but it doesn't tell us how spread out values are.

\- For example, take

$$
X = 0 \text { with   probability } 1
$$

$$
Y = \left\{ \begin{array}{l l} - 1 & p = \frac {1}{2} \\ 1 & p = \frac {1}{2} \end{array} \right.
$$

$$
Z = \left\{ \begin{array}{l l} - 1 0 0 & p = \frac {1}{2} \\ 1 0 0 & p = \frac {1}{2} \end{array} \right..
$$

- What are the expected values? 0, 0, 0.  
- But there is much greater spread in $Z$ than $Y$ and $Y$ than $X$ . Thus expectation is not enough to detect spread, or variation.

DEFINITION. If $X$ is a r.v with mean $\mu = \mathbb{E}X$ , then the variance of $X$ , denoted by $\operatorname{Var}(X)$ , is defined by

$$
\operatorname{Var} (X) = \mathbb {E} \left[ (X - \mu) ^ {2} \right].
$$

- Remark: $\mathbb{E}c = c$ .  
- We prove an alternate formula for the variance. (The technique of using linearity is important here!!! Hint Hint)

$$
\operatorname{Var} (X) = \mathbb {E} \left[ (X - \mu) ^ {2} \right]
$$

$$
= \mathbb {E} \left[ X ^ {2} - 2 \mu X + \mu^ {2} \right]
$$

$$
= \mathbb {E} [ X ^ {2} ] - 2 \mu \mathbb {E} [ X ] + \mathbb {E} [ \mu^ {2} ]
$$

$$
= \mathbb {E} \left[ X ^ {2} \right] - 2 \mu^ {2} + \mu^ {2}
$$

$$
= \mathbb {E} \left[ X ^ {2} \right] - \mu^ {2}.
$$

THEOREM. We have that

$$
\operatorname{Var} (X) = \mathbb {E} [ X ^ {2} ] - (\mathbb {E} [ X ]) ^ {2}.
$$

\- Example1: Calculate $\operatorname{Var}(X)$ if $X$ represents the outcome when a fair die is rolled.

\- Solution: Previously we calculated that $\mathbb{E}X = \frac{7}{2}$ .

\- Thus we only need to calculate the second moment:

$$
\mathbb {E} X ^ {2} = 1 ^ {2} \left(\frac {1}{6}\right) + \dots + 6 ^ {2} \frac {1}{6}
$$

$$
= \frac {9 1}{6}.
$$

\- Using our formula we have that

$$
\begin{array}{l} \operatorname{Var} (X) = \mathbb {E} [ X ^ {2} ] - (\mathbb {E} [ X ]) ^ {2} \\ = \frac {9 1}{6} - \left(\frac {7}{2}\right) ^ {2} \\ = \frac {3 5}{1 2}. \\ \end{array}
$$

• Here is a useful formula:

PROPOSITION 7. For constants $a, b$ we have that $\operatorname{Var}(aX + b) = a^2\operatorname{Var}(X)$ .

PROOF. We compute

$$
\begin{array}{l} \operatorname{Var} (a X + b) = \mathbb {E} \left[ (a X + b - \mathbb {E} [ a X + b ]) ^ {2} \right] \\ = \mathbb {E} \left[ (a X + b - a \mu - b) ^ {2} \right] \\ = \mathbb {E} \left[ a ^ {2} (X - \mu) ^ {2} \right] \\ = a ^ {2} \mathbb {E} \left[ (X - \mu) ^ {2} \right] \\ = a ^ {2} \operatorname{Var} (X). \\ \end{array}
$$

DEFINITION. We define

$$
S D (X) = \sqrt {\operatorname{Var} (X)}
$$

to be the standard deviation of $X$ .

![](images/275c260817f53b0c5226c9641d495030dbd3e5584cdba4cdfdfb77829dd4dcd2.jpg)

## CHAPTER 6

# Some Discrete Distributions

## 6.1. Bernouli and Binomial Random Variables

## • Bernoulli Distribution

- Suppose that a trial or experiment takes place, whose outcome is either success or failure.  
- Let $X = 1$ when the outcome is a success and $X = 0$ if it is a failure.  
- The pmf of $X$ is given by

$$
p (0) = \mathbb {P} (X = 0) = 1 - p
$$

$$
p (1) = \mathbb {P} (X = 1) = p
$$

where $0 \leq p \leq 1$ .

\- For this $X$ , $X$ is said to be a Bernoulli random variable with parameter $p$ ,

\* We wrtie this as $X \sim \text{Bernoulli}(p)$ ,  
\* Properties:

$\cdot \mathbb{E}X = p\cdot 1 + (1 - p)\cdot 0 = p$  
$\cdot\mathbb{E}X^{2}=1^{2}\cdot p+0^{2}(1-p)=p.$  
- So $\operatorname{Var} X = p - p^2 = p(1 - p)$ .

## • Binomial Distribution:

\- We say $X$ has a binomial distribution with parameters $n$ and $p$ if

$$
p _ {X} (k) = \mathbb {P} \left(X = k\right) = \binom{n}{k} p ^ {k} \left(1 - p\right) ^ {n - k}.
$$

\- Interpret: $X =$ the number of successes in $n$ independent trials.

\* Let's take this as given.

\- We say $X \sim \text{Binomial}(n, p)$ or $X \sim \text{bin}(n, p)$ .

## • Properties of the Binomial

\- Check that probabilities sums to 1: Not really a property but more of a check that $X$ is indeed a random variable:

\* We need to check two things:

(1) That $p_X(k) \geq 0$ , and this is obvious from the formula  
(2) Need to check that $\sum_{k=0}^{n} p_X(k) = 1$ .

\* First recall the Binomial Theorem: $\sum_{k=0}^{n}\binom{n}{k}x^k y^{n-k} = (x+y)^n$ .

\- Then

$$
\sum_ {k = 0} ^ {n} p _ {X} (k) = \sum_ {k = 0} ^ {n} \binom{n}{k} p ^ {k}   (1 - p) ^ {n - k} = (p + (1 - p)) ^ {n} = 1 ^ {n} = 1.
$$

\- Mean: Easiest way to compute $\mathbb{E}X$ is by recognizing that $X = Y_{1} + \dots + Y_{n}$ where $Y_{i}$ are independent Bernoulli's.

\* Thus $EX = EY_{1} + \cdots + EY_{n} = p + \cdots + p = np.$  
\* We can do this directly too, but this would involve proving that

$$
\mathbb {E} X = \sum_ {k = 0} ^ {n} k p (k) = n p,
$$

meaning we would have to prove

$$
\sum_ {k = 0} ^ {n} k \binom{n}{k} p ^ {k} (1 - p) ^ {n - k} = n p.
$$

\- Variance: We first compute the second moment. As before write $X = Y_{1} + \cdots + Y_{n}$ where $Y_{i}$ are bernoulli's.

$$
\begin{array}{l} \mathbb {E} X ^ {2} = \mathbb {E} \left(Y _ {1} + \dots + Y _ {n}\right) ^ {2} \\ = \sum_ {k = 1} ^ {n} \mathbb {E} Y _ {k} ^ {2} + \sum_ {i \neq j} \mathbb {E} \left[ Y _ {i} Y _ {j} \right] \\ = \sum_ {k = 1} ^ {n} p + \sum_ {i \neq j} \mathbb {E} \left[ Y _ {i} Y _ {j} \right] \\ = n p + \sum_ {i \neq j} \mathbb {E} \left[ Y _ {i} Y _ {j} \right] \\ = (\star) \\ \end{array}
$$

\* Now each term $\mathbb{E}[Y_i Y_j]$ for fixed $i, j$ can be computed as =

$$
\begin{array}{l} \mathbb {E} \left[ Y _ {i} Y _ {j} \right] = 1 \cdot \mathbb {P} \left(Y _ {i} Y _ {j} = 1\right) + 0 \cdot \mathbb {P} \left(Y _ {i} Y _ {j} = 0\right) \\ = \mathbb {P} ((Y _ {i} = 1) \cap (Y _ {j} = 1)) \\ = \mathbb {P} (Y _ {i} = 1) \mathbb {P} (Y _ {j} = 1), \text { by   independence } \\ = p ^ {2}. \\ \end{array}
$$

\* Now there are a total of $n^2$ terms is $(Y_1 + \dots + Y_n)^2$ , $n$ of which are of form $Y_k^2$ . Thus there are $n^2 - n$ terms of the form $Y_iY_j$ with $i \neq j$ .

\* Hence using $(\star)$ we have $\mathbb{E}X^2 = np + (n^2 - n)p^2$ .

\* Thus

$$
\operatorname{Var} X = \mathbb {E} X ^ {2} - (\mathbb {E} X) ^ {2} = n p + (n ^ {2} - n) p ^ {2} - (n p) ^ {2} = n p (1 - p).
$$

\- Sumarize: $\mathbb{E}X = np$ and $\operatorname{Var}X = np(1 - p)$ .

\- $\underline{\text{Moments}}$ : We can also prove $\mathbb{E}X^k = np\mathbb{E}\left[(Y + 1)^{k - 1}\right]$ .

\- Calculator(TI-84):

\- 2ndDistri>binomialpdf(n,p,x)=P(X=x).

\- same with cdf.

\- Example1: A company prices its hurricane insurance using the following assumptions:

\- (i) In any calendar year, there can be at most one hurricane.

\- (ii) In any calendar year, the probability of a hurricane is 0.05.

\- (iii) The numbers of hurricanes in different calendar years are mutually independent. Using the company's assumptions, calculate the probability that there are fewer than 3 hurricanes in a 20-year period

\- Solution: We have that $X \sim \text{bin}(20, .05)$ then

$$
\begin{array}{l} \mathbb {P} (X <   3) = \mathbb {P} (X \leq 2) \\ = \binom{2 0}{0} (. 0 5) ^ {0} (. 9 5) ^ {2 0} + \binom{2 0}{1} (. 0 5) ^ {1} (. 9 5) ^ {1 9} + \binom{2 0}{2} (. 0 5) ^ {2} (. 9 5) ^ {1 2} \\ = . 9 2 4 5. \\ \end{array}
$$

\- Example2: Phan has a .6 probability of making a free throw. Suppose each free throw is independent of the other. If he attempts 10 free throws, what is the probability that he makes at least 2 of them?

\- Solution: Let $X \sim \text{bin}(10, .6)$ then

$$
\begin{array}{l} \mathbb {P} (X \geq 2) = 1 - \mathbb {P} (X = 0) - \mathbb {P} (X = 1) \\ = 1 - \binom{1 0}{0} (. 6) ^ {0} (. 4) ^ {1 0} - \binom{1 0}{1} (. 6) ^ {1} (. 4) ^ {9} \\ = . 9 9 8. \\ \end{array}
$$

## 6.2. The Poisson Distribution

\- We say that $X = 0,1,2,\ldots$ is Poisson with parameter $\lambda > 0$ if

$$
p _ {X} (i) = \mathbb {P} (X = i) = e ^ {- \lambda} \frac {\lambda^ {i}}{i !} \text { for } i = 0, 1, 2, 3, \dots .
$$

\- Or $X \sim \mathrm{Poisson}(\lambda)$ .

\- In general Poisson random variables are of the following form

\- Suppose success happens $\lambda$ times on average in a given period (per year, per month etc). Then $X =$ number of times success happens in that given period.

\- Possion is like binomial, expect, $X$ is infinitely countable!

• Examples that obey Poisson R.V

\- 1. The number of misprints on a page ogf a book

\- 2. # of people in community that survive to age 100

\- 3. # of telephone numbers that are dialed in a day.

\- 4. # of customers entering post office on a day.

\- Calc2: Recall that $\sum_{n=0}^{\infty} \frac{x^n}{n!} = e^x$ .

\- Properties of Poisson: Let $X \sim \text{Poisson}(\lambda)$

\- First we check that $p_X(i)$ is indeed a pmf: First it is obvious that $p_X(i) \geq 0$ since $\lambda > 0$ . We to need to check that all the probabilities add up to one:

$$
\sum_ {i = 0} ^ {\infty} p _ {X} (i) = \sum_ {i = 0} ^ {\infty} e ^ {- \lambda} \frac {\lambda^ {i}}{i !} = e ^ {- \lambda} \sum_ {i = 0} ^ {\infty} \frac {\lambda^ {i}}{i !} = e ^ {- \lambda} e ^ {\lambda} = 1.
$$

\- Mean: We have

$$
\mathbb {E} X = \sum_ {i = 0} ^ {\infty} i e ^ {- \lambda} \frac {\lambda^ {i}}{i !} = e ^ {- \lambda} \lambda \sum_ {i = 1} ^ {\infty} \frac {\lambda^ {i - 1}}{(i - 1) !}
$$

$$
= e ^ {- \lambda} \lambda e ^ {\lambda} = \lambda .
$$

\- Variance: We first have

$$
\begin{array}{l} \mathbb {E} X ^ {2} = \sum_ {i = 0} ^ {\infty} i ^ {2} \frac {e ^ {- \lambda} \lambda^ {i}}{i !} \\ = \lambda \sum_ {i = 0} ^ {\infty} i \frac {e ^ {- \lambda} \lambda^ {i - 1}}{(i - 1) !} \\ = \lambda \sum_ {j = 0} ^ {\infty} (j + 1) \frac {e ^ {- \lambda} \lambda^ {j}}{j !}, \text {   let   } j = i - 1 \\ = \lambda \left[ \sum_ {j = 0} ^ {\infty} j \frac {e ^ {- \lambda} \lambda^ {j}}{j !} + \sum_ {j = 0} ^ {\infty} \frac {e ^ {- \lambda} \lambda^ {j}}{j !} \right] \\ = \lambda [ \lambda + e ^ {- \lambda} e ^ {\lambda} ] \\ = \lambda (\lambda + 1). \\ \end{array}
$$

Thus

$$
\operatorname{Var} X = \lambda (\lambda + 1) - \lambda^ {2} = \lambda .
$$

\- Example1: Suppose on average there are 5 homicides per month in Hartford, CT. What is the probability there will be at most 1 in a certain month?

\- Answer: If $X$ is the number of homicides, we are given that $EX = 5$ . Since the expectation for a Poisson is $\lambda = 5$ . Therefore $P(X = 0) + P(X = 1) = e^{-5} + 5e^{-5}$ .

\- Example2: Suppose on average there is one large earthquake per year in Mexico. What's the probability that next year there will be exactly 2 large earthquakes?

\- $\underline{\text{Answer:}}\lambda = EX = 1$ , so $P(X = 2) = \frac{e^{-1}}{2}$ .

\- Example3: Phan receives texts on the average of two every 3 minutes. Assume Poisson.

– Question: What is the probability of five or more texts arriving in a 9-minute period.

\- Answer: Let $X$ number of calls in a 9-minute period. Let $n =$ number of periods, $\lambda_1 = 2$ . Thus $\lambda = 3 \cdot 2 = 6$ . Thus

$$
\begin{array}{l} \mathbb {P} (X \geq 5) = 1 - \mathbb {P} (X \leq 4) \\ = 1 - \sum_ {n = 0} ^ {4} \frac {e ^ {- 6} 6 ^ {n}}{n !} \\ = 1 -. 2 8 5 = . 7 1 5. \\ \end{array}
$$

\- Important: Poisson is similar to Binomial in the following way

\- FACT: Poisson approximates $Bin(n,p)$ when $n$ is large and $p$ is small enough so that $np$ is of moderate size.

THEOREM 8. If $X_{n}$ is binomial with parameters $n$ and $p_n$ and $np_n \to \lambda$ , then

$$
\mathbb {P} \left(X _ {n} = i\right)\rightarrow \mathbb {P} (Y = i)
$$

where $Y \sim \text{Poisson}(\lambda)$ .

PROOF. See class textbook.

![](images/cd064e0ec628e2ab5741778dfd20bd09eb428c877936240cb08730f39a214f00.jpg)

\- Summary of Theorem: This theorem says that suppose $n$ is large and $p$ is small, Thus
  - If $X \sim Bin(n, p)$ then we approximate $X$ with a possession by letting let $\lambda = np$ so that

$$
\mathbb {P} (X = i) \approx e ^ {- n p} \frac {(n p) ^ {i}}{i !}.
$$

\- When can we assume $X$ is Poisson: Another consequence of this theorem says that when $Y =$ "the number of successes in a given period". And if the number possible of trials $n$ is large, and if the probability $p$ of success is small, then $Y$ can be treated as a Poisson random variable.

NOTE:

\- (1)Why is number of misprints on a page will be approximately Poisson with $\lambda = np$

\* Let $X =$ number of misprints on a page of a book.

\* Since prob of error, say $p = .01$ is usually small, and number of letters on a page is usually large, say $n = 1000$ . Then the average is $\lambda = np$ .

\* Then because $p$ is small and $n$ is large, then $X$ can be approximated by a Poisson.

\- (2) Let $X$ number of accidents in a year

\* $X$ is Poisson because the probability of an accident $p$ in a given period is usually small and while the number $n$ of times someone drives in a given period is high.

• Example: Here is an example showing this.

\- If $X$ is number of times you get heads on a biased coin where $\mathbb{P}(H) = \frac{1}{100}$ . Suppose you you toss 1000 times. Then $np = 10$

$$
\mathbb {P} (X = 5) \approx e ^ {- 1 0} \frac {1 0 ^ {5}}{5 !} = . 0 3 7 8
$$

while the actual value is

$$
\begin{array}{l} \mathbb {P} \left(X = 5\right) = \binom{1 0 0 0}{5} (. 0 1) ^ {5} (. 9 9) ^ {9 9 5} \\ = \frac {1 0 0 0 !}{9 9 5 ! 5 !} (. 0 1) ^ {5} (. 9 9) ^ {9 9 5} \\ = . 0 3 7 5. \\ \end{array}
$$

## 6.3. Other Discrete Distributions

## • Uniform Distribution:

\- We say $X$ is uniform, and write this as $X \sim \text{uniform}(n)$ , if $X \in \{1, 2, \ldots, n\}$ and

$$
p _ {X} (i) = \mathbb {P} (X = i) = \frac {1}{n} \text { for } i = 1, 2, \dots , n.
$$

\- $\underline{\text{Exercise:}}$ $\mathbb{E}X = \sum_{i=1}^{n} i\frac{1}{n} = \frac{1}{n}\sum_{i=1}^{n} i = \frac{1}{n}\frac{n(n-1)}{2} = \frac{n-1}{2}$ and find $\operatorname{Var}X$ .

## - Geometric Distribution:

- Experiment: Suppose that independent trials are held until success occurs. Trials are stopped once success happens. Let $p$ be the probability of having a success in each trial.  
- Let $X =$ "number of trials required until first success occurs". Thus $X \in \{1,2,3,4,\ldots\}$ Here we have

$$
p _ {X} (i) = \mathbb {P} (X = i) = (1 - p) ^ {i - 1} p \text {for} i = 1, 2, 3, 4 \dots .
$$

- We say $X \sim \operatorname{geometric}(p)$ .  
- Properties:

\* We first double check is indeed a discrete random variable: This follows from what we know about geometric series:

$$
\sum_ {i = 1} ^ {\infty} \mathbb {P} (X = i) = \sum_ {i = 1} ^ {\infty} (1 - p) ^ {i - 1} p = \frac {p}{1 - (1 - p)} = 1.
$$

\* Mean: Recall that by differentiation of the geometric series, we came up with the formula $\sum_{n=0}^{\infty} nx^{n-1} = \frac{1}{(1-x)^2}$ , so that

$$
\begin{array}{l} \mathbb {E} X = \sum_ {i = 1} ^ {\infty} i \mathbb {P} (X = i) \\ = \sum_ {i = 1} ^ {\infty} i (1 - p) ^ {i - 1} p \\ = \frac {p}{(1 - (1 - p)) ^ {2}} = \frac {1}{p}. \\ \end{array}
$$

\* Variance:(Leave as Exercise for student) Note that

$$
\mathbb {E} X ^ {2} = \sum_ {i = 1} ^ {\infty} i ^ {2} (1 - p) ^ {i - 1} p. (\star)
$$

Thus we can differentiate $\sum_{n=1}^{\infty} nx^{n-1} = \frac{1}{(1-x)^2}$ again to get $\sum_{n=2}^{\infty} n(n-1)x^{n-2} = \frac{2}{(1-x)^3}$ .

\* From this we will attempt to get $EX^{2}$ in ( $\star$ ) by splitting the sum up:

$$
\sum_ {n = 2} ^ {\infty} n (n - 1) (1 - p) ^ {n - 2} = \frac {2}{(1 - (1 - p)) ^ {3}} = \frac {2}{p ^ {3}},
$$

$$
\sum_ {n = 2} ^ {\infty} n (n - 1) (1 - p) ^ {n - 2} p = \frac {2}{p ^ {2}}, \text { now   split },
$$

$$
\sum_ {n = 2} ^ {\infty} n ^ {2} (1 - p) ^ {n - 2} p = \frac {2}{p ^ {2}} + \sum_ {n = 2} ^ {\infty} n (1 - p) ^ {n - 2} p
$$

$$
(1 - p) ^ {- 1} \sum_ {n = 1} ^ {\infty} n ^ {2} (1 - p) ^ {n - 1} p = \frac {2}{p ^ {2}} + \sum_ {n = 2} ^ {\infty} n (1 - p) ^ {n - 2} p + (1 - p) ^ {- 1} p
$$

$$
\begin{array}{l} (1 - p) ^ {- 1} \mathbb {E} X ^ {2} = \frac {2}{p ^ {2}} + (1 - p) ^ {- 1} \sum_ {n = 1} ^ {\infty} n (1 - p) ^ {n - 1} p \\ = \frac {2}{p ^ {2}} + (1 - p) ^ {- 1} \frac {1}{p} \\ \end{array}
$$

Thus

$$
\begin{array}{l} \mathbb {E} X ^ {2} = \frac {2 (1 - p)}{p ^ {2}} + \frac {1}{p} \\ = \frac {2 - 2 p + p}{p ^ {2}} = \frac {2 - p}{p ^ {2}} \\ \end{array}
$$

\* So Thus

$$
\begin{array}{l} \operatorname{Var} X = \mathbb {E} X ^ {2} - (\mathbb {E} X) ^ {2} = \frac {2 - p}{p ^ {2}} - \frac {1}{p ^ {2}} \\ = \frac {(1 - p)}{p ^ {2}} \\ \end{array}
$$

\- Example1: An urn contains 10 white balls and 15 black balls. Balls are randomly selected, one at a time, until a black one is obtained. If we assume that each ball selected is replaced before the next one is drawn, what is the probability.

\- Part (a): Exactly 6 draws are needed?

\* X = number of draws needed to select a black ball, the probability of success is

$$
p = \frac {1 5}{1 0 + 1 5} = \frac {1 5}{2 5} = . 6.
$$

\* Thus

$$
\mathbb {P} (X = 6) = (. 4) ^ {6 - 1} (. 6) = . 0 0 6 1 4 4
$$

\- Part (a): What is the expected number of draws in this game?

\* Since $X \sim$ geometric(.6) then

$$
\mathbb {E} X = \frac {1}{p} = \frac {1 0}{6} = 1. \bar {6}
$$

\- Part (c)(Extra Problem to be done at home) Find exactly that probability at least $k$ draws are needed?

\* We have that

$$
\begin{array}{l} \mathbb {P} (X \geq k) = \sum_ {n = k} ^ {\infty} \mathbb {P} (X = k) \\ = \sum_ {n = k} ^ {\infty} (. 4) ^ {n - 1} (. 6) \\ = (. 6) (. 4) ^ {- 1} \sum_ {n = k} ^ {\infty} (. 4) ^ {n} \\ = (. 6) (. 4) ^ {- 1} (. 4) ^ {k} \sum_ {n = 0} ^ {\infty} (. 4) ^ {n} \\ = (. 6) (. 4) ^ {k - 1} \frac {1}{1 - . 4} \\ = (. 4) ^ {k - 1}. \\ \end{array}
$$

\- Note: This could have been done for a general $p$ . Thus

$$
\mathbb {P} (X \geq k) = (1 - p) ^ {k - 1}.
$$

\- Negative Binomial(Need to know for Actuarial Exam):

\- Experiment: Suppose that independent trials are held with probability $p$ of having a success. The trials are perfomed until a total of $r$ successes are accumulated.

\* Let $X$ equal the number of trials required to obtain $r$ successes. Here we have

$$
\mathbb {P} \left(X = n\right) = \binom{n - 1}{r - 1} p ^ {r} \left(1 - p\right) ^ {n - r} \text {   for   } n = r, r + 1, \ldots .
$$

- We say $X \sim \text{NegativeBinomial}(r, p)$ .  
- Properties:

\* This is a probability mass function. Can check that $\sum_{n=r}^{\infty}\mathbb{P}(X=n)=1$ .

\* Mean:

$$
\mathbb {E} X = \frac {r}{p}.
$$

\* Variance:

$$
\operatorname{Var} (X) = \frac {r (1 - p)}{p ^ {2}}.
$$

\- Note that $\operatorname{Geometric}(p) = \text{NegativeBinomial}(1, p)$ .

\- Example: Find the expected value of the number of times one must throw a die until the outcome 1 has occurred 4 times.

\- Solution: $X \sim \text{NegativeBinomial}\left(4, \frac{1}{6}\right)$ . So

$$
\mathbb {E} X = \frac {4}{\frac {1}{6}} = 2 4.
$$

• Hypergeometric Distribution(Need to know for Actuarial Exam):

\- Experiment: Suppose that a sample of size $n$ is to be chosen randomly (without replacement) from an urn containing $N$ balls, of which $m$ are white and $N - m$ are black.

\* Let $X$ equal the number of white balls selected. Then

$$
\mathbb {P} \left(X = i\right) = \frac {\binom{m}{i} \binom{N - m}{n - i}}{\binom{N}{n}} \text {   for   } n = 0, 1, \ldots , n.
$$

\- We say $X \sim \text{Hypergeometric}(n, N, m)$ .

\- Properties:

\* Mean:

$$
\mathbb {E} X = \frac {n m}{N}.
$$

\* Variance:

$$
\operatorname{Var} (X) = n \frac {m}{N} \left(1 - \frac {m}{N}\right) \left(1 - \frac {n - 1}{N - 1}\right).
$$

## CHAPTER 7

# Continuous Random Variables

## 7.1. Intro to continuous R.V

DEFINITION. A random variable $X$ is said to have a continuous distribution if there exists a non-negative function $f$ such that

$$
\mathbb {P} (a \leq X \leq b) = \int_ {a} ^ {b} f (x) d x
$$

for every $a$ and $b$ . [Sometimes we write that for nice sets $B \subset \mathbb{R}$ we have $\mathbb{P}(X \in B) = \int_{B} f(x) dx$ .]

We call $f$ the pdf (probability density function) for $X$ . Sometime we we the notation $f_{X}$ to signify $f_{X}$ corresponds to the pdf of $X$ . We sometimes call $f_{X}$ the density of $X$ .

\- In fact, any function $f$ satisfying the following two properties is called a density, and could be considered a pdf of some random variable $X$ :

(1) $f(x)\geq 0$ for all $x$  
(2) $\int_{-\infty}^{\infty} f(x) dx = 1$ .

\- Important Note!

- (1) In this case $X: S \to \mathbb{R}$ and the could attain uncountably many values (doesn't have to discrete)  
- (2) $\int_{-\infty}^{\infty} f(x) dx = \mathbb{P}(-\infty < X < \infty) = 1$ .  
- (3) $\mathbb{P}(X = a) = \int_{a}^{a} f(x) dx = 0$ .  
- (4) $\mathbb{P}(X < a) = \mathbb{P}(X \leq a) = F(a) = \int_{-\infty}^{a} f(x) dx$ .

\* Recall that $F$ is the cdf of $X$ .

\- (5) Draw a pdf of $X$

\* Note that $\mathbb{P}(a < X < b)$ is just the area under the curve.

\- Remark: What are some random variables that are considered continuous?

- Let $X$ be the time it takes it take for a student to finish a probability exam. $X \in (0, \infty)$ .  
- Let $X$ be the value of a Apple's stock price at the end of the day. Again $X \in [0, \infty)$ .  
- Let $X$ be the height of a college student.  
- Any sort of continuous measurement can be considered a continuous random variable.

• Example1: Suppose we are given

$$
f (x) = \left\{ \begin{array}{l l} \frac {c}{x ^ {3}} & x \geq 1 \\ 0 & x <   1 \end{array} \right.
$$

is the pdf of $X$ . What must the value of $c$ be?

\- Solution: We would need

$$
1 = \int_ {- \infty} ^ {\infty} f (x) d x = c \int_ {1} ^ {\infty} \frac {1}{x ^ {3}} d x = \frac {c}{2},
$$

thus c = 2.

• Example2: Suppose we are given

$$
f _ {X} (x) = \left\{ \begin{array}{l l} \frac {2}{x ^ {3}} & x \geq 1 \\ 0 & x <   1 \end{array} \right.
$$

is the pdf of $X$ from Example1.

\- Part (a): Find the c.d.f, $F_{X}(x)$ .

\* Solution: First we check that if x < 1 then

$$
F _ {x} (x) = \mathbb {P} (X \leq x) = \int_ {- \infty} ^ {x} f _ {X} (y) d y = \int_ {- \infty} ^ {x} 0 d y = 0.
$$

Now when $x \geq 1$ we have

$$
\begin{array}{l} F _ {X} (x) = \mathbb {P} (X \leq x) = \int_ {- \infty} ^ {x} f _ {X} (y) d y \\ = \int_ {- \infty} ^ {1} 0 d y + \int_ {1} ^ {x} \frac {2}{y ^ {3}} d y \\ = \int_ {1} ^ {x} \frac {2}{y ^ {3}} d y \\ = 1 - \frac {1}{x ^ {2}}. \\ \end{array}
$$

thus

$$
F _ {X} (x) = \left\{ \begin{array}{l l} 1 - \frac {1}{x ^ {2}} & x \geq 1 \\ x & x <   1 \end{array} \right.
$$

\- Part (b): Use the cdf in Part (a) to help you find $\mathbb{P}(3\leq X\leq 4)$ .

\* Solution: We have

$$
\begin{array}{l} \mathbb {P} (3 \leq X \leq 4) = \mathbb {P} (X \leq 4) - \mathbb {P} (X <   3) \\ = F _ {X} (4) - F _ {X} (3) \\ = \left(1 - \frac {1}{4 ^ {2}}\right) - \left(1 - \frac {1}{3 ^ {2}}\right) = \frac {7}{1 4 4}. \\ \end{array}
$$

\- Fact: For continuous R.V we have the following useful relationship

\- Since $F(x) = \int_{-\infty}^{x} f(y) dy$ then by the fundament at theorem of calculus (Do you remember this form Calculus 1 or 2?)

$$
F ^ {\prime} (x) = f (x).
$$

\- This means that for continuous random variables, the derivative of the CDF is the PDF!

\- Example3: Let

$$
f (x) = \left\{ \begin{array}{l l} c e ^ {- 2 x} & x \geq 0 \\ 0 & x <   0 \end{array} \right.
$$

Find c.

\- Solution: $c = 2$ .

## 7.2. Expectation and Variance

\- Recall that if $p(x)$ is the pmf (density) of a discrete random variable, we had

$$
\mathbb {E} X = \sum_ {i = 1} ^ {\infty} x _ {i} p (x _ {i}).
$$

DEFINITION. If $X$ is continuous with density $f(x)$ then

$$
\mathbb {E} X = \int_ {- \infty} ^ {\infty} x f (x) d x.
$$

• Example1: Suppose X has density

$$
f (x) = \left\{ \begin{array}{l l} 2 x & 0 \leq x \leq 1 \\ 0 & \text { otherwise } \end{array} \right..
$$

Find $\mathbb{E}X$ .

\- Solution: We have that

$$
\begin{array}{l} \mathbb {E} [ X ] = \int_ {- \infty} ^ {\infty} x f (x) d x \\ = \int_ {0} ^ {1} x \cdot 2 x d x \\ = \frac {2}{3}. \\ \end{array}
$$

THEOREM 9. If $X$ and $Y$ are continuous random variable then

(a) $\mathbb{E}[X + Y] = \mathbb{E}X + \mathbb{E}Y.$  
(b) $E[aX]=aE X$ where $a\in R$ .

PROOF. See textbook. It will be shown later.

![](images/1e610fd0cd4de09f82ce873bfc6d251bb82d73d0948434e96e152f0298f14cc0.jpg)

PROPOSITION. If $X$ is a continuous $R.V.$ with pdf $f(x)$ , then for any real valued function $g$ ,

$$
\mathbb {E} \left[ g (X) \right] = \int_ {- \infty} ^ {\infty} g (x) f (x) d x.
$$

• Example2: The density of X is given by

$$
f (x) = \left\{ \begin{array}{l l} \frac {1}{2} & \text { if } 0 \leq x \leq 2 \\ 0 & \text { otherwise } \end{array} \right..
$$

Find $\mathbb{E}\left[e^{X}\right]$ .

\- Solution: From the previous proposition we have that $g(x) = e^{x}$ in this case thus

$$
\mathbb {E} e ^ {X} = \int_ {0} ^ {2} e ^ {x} \cdot \frac {1}{2} d x = \frac {1}{2} \left[ e ^ {2} - 1 \right].
$$

LEMMA 10. For nonnegative random variable $Y \geq 0$ we have

$$
\mathbb {E} Y = \int_ {0} ^ {\infty} \mathbb {P} (Y > y) d y.
$$

\- Bonus:

\- This proof is a good practice with interchanging order of integrals in Multivariable Calculus.

PROOF. Recall that dxdy means Right-Left and dydx means Top-Bottom.

$$
\begin{array}{l} \int_ {0} ^ {\infty} \mathbb {P} (Y > y) d y = \int_ {0} ^ {\infty} \int_ {y} ^ {\infty} f _ {Y} (x) d x d y \\ = \int \int_ {D} f _ {Y} (x) d y d x, \text {   interchange   order   in   Calc   III   } \\ = \int_ {0} ^ {\infty} \int_ {0} ^ {x} f _ {Y} (x) d y d x \text { draw   the   region   to   do   this } \\ = \int_ {0} ^ {\infty} x f _ {Y} (x) d x \\ = \mathbb {E} X. \\ \end{array}
$$

\- Variance:

\- Will be define in the same way as we did with discrete random variable:

$$
\operatorname{Var} (X) = \mathbb {E} \left[ (X - \mu) ^ {2} \right]
$$

$$
\operatorname{Var} (X) = \mathbb {E} X ^ {2} - (\mathbb {E} X) ^ {2}.
$$

\- As before

$$
\operatorname{Var} (a X + b) = a ^ {2} \operatorname{Var} (X).
$$

\- Example3: (Example 1 continued) Suppose $X$ has density

$$
f (x) = \left\{ \begin{array}{l l} 2 x & 0 \leq x \leq 1 \\ 0 & \text { otherwise } \end{array} \right..
$$

Find $\operatorname{Var}(X)$ .

\- Solution: From Example 1 we found $\mathbb{E}[X] = \frac{2}{3}$ . Now

$$
\begin{array}{l} \mathbb {E} \left[ X ^ {2} \right] = \int_ {0} ^ {1} x ^ {2} \cdot 2 x d x = 2 \int_ {0} ^ {1} x ^ {3} d x \\ = \frac {1}{2}. \\ \end{array}
$$

Thus

$$
\operatorname{Var} (X) = \frac {1}{2} - \left(\frac {2}{3}\right) ^ {2} = \frac {1}{1 8}.
$$

• Example4: Suppose X has density

$$
f (x) = \left\{ \begin{array}{l l} a x + b & 0 \leq x \leq 1 \\ 0 & \text { otherwise } \end{array} \right..
$$

and that $\mathbb{E}\left[X^2\right] = \frac{1}{6}$ . Find the values of $a$ and $b$ .

\- Solution: We need to use the fact that $\int_{-\infty}^{\infty}f(x)dx = 1$ and $\mathbb{E}\left[X^2\right] = \frac{1}{6}$ . The first one gives us,

$$
1 = \int_ {0} ^ {1} (a x + b) d x = \frac {a}{2} + b
$$

and the second one give us

$$
\frac {1}{6} = \int_ {0} ^ {1} x ^ {2} (a x + b) d x = \frac {a}{4} + \frac {b}{3}.
$$

Solving these equations gives us

$$
a = - 2, \text {   and   } b = 2.
$$

## 7.3. The uniform Random Variable

\- A continuous random variable is said to be uniformly distributed on the interval $[a, b]$ if

$$
f _ {X} (x) = \left\{ \begin{array}{l l} \frac {1}{b - a} & a \leq x \leq b \\ 0 & \text { otherwise } \end{array} \right..
$$

- So $X$ can only attain values in $X \in [a, b]$ .  
- We say $X \sim \text{Uniform}(a, b)$ .  
- The cdf is

$$
F _ {X} (x) = \left\{ \begin{array}{l l} 0 & x <   a \\ \frac {x - a}{b - a} & a \leq x \leq b \\ 1 & x > b \end{array} \right..
$$

\- Example1: Suppose $X \sim \text{Uniform}(a, b)$ Part (a) Find the mean of $X$ . Part (b) Find the variance of $X$ .

\- Part (a): We compute

$$
\begin{array}{l} \mathbb {E} X = \int_ {- \infty} ^ {\infty} x f _ {X} (x) d x = \int_ {a} ^ {b} x \frac {1}{b - a} d x \\ = \frac {1}{b - a} \left(\frac {b ^ {2}}{2} - \frac {a ^ {2}}{2}\right) = \frac {a + b}{2}. \\ \end{array}
$$

\* Which makes sense right? It should be the midpoint of the interval $[a, b]$ .

\- Part(b): We compute first the second moment

$$
\begin{array}{l} \mathbb {E} X ^ {2} = \int_ {a} ^ {b} x ^ {2} \frac {1}{b - a} d x = \frac {1}{b - a} \left(\frac {b ^ {3}}{3} - \frac {a ^ {3}}{3}\right) \\ = \frac {1}{3} \frac {1}{b - a} (b - a) (a ^ {2} + a b + b ^ {2}) \\ = \frac {a ^ {2} + a b + b ^ {2}}{3}. \\ \end{array}
$$

Thus after some algebra

$$
\operatorname{Var} X = \frac {a ^ {2} + a b + b ^ {2}}{3} - \left(\frac {a + b}{2}\right) ^ {2} = \frac {(b - a) ^ {2}}{1 2}.
$$

## 7.4. More practice

\- Suppose we are given the pd.f.

$$
f (x) = \left\{ \begin{array}{l l} 9 e ^ {- 9 x} & x \geq 0 \\ 0 & x <   0 \end{array} \right.
$$

\- Part (a): Set up integral to find $F_{X}(x)$ :

\* We have for $x > 0$ , that

$$
F _ {X} (x) = \int_ {0} ^ {x} 9 e ^ {- 9 y} d y = 1 - e ^ {- 9 x},
$$

so that

$$
F _ {X} (x) = \left\{ \begin{array}{l l} 1 - e ^ {- 9 x} & x \geq 0 \\ 0 & x <   0 \end{array} \right. /
$$

\- Part (b): Set up integral to find $\mathbb{P}(1 < X < 5)$

$$
* \int_ {1} ^ {5} 9 e ^ {- 9 x} d x
$$

\- Part (c): Set up integral to find $\mathbb{P}(X > 3)$

$$
* \int_ {3} ^ {\infty} 9 e ^ {- 9 x} d x.
$$

\- Part (s): Set up integral to find $\mathbb{P}(X < 2)$

$$
* \int_ {0} ^ {2} 9 e ^ {- 9 x} d x.
$$

## CHAPTER 8

## Normal Distributions

## 8.1. The normal distribution

\- We say that $X$ is a normal (Gaussian) random variable, or $X$ is normally distributed with parameters $\mu$ and $\sigma^2$ if the density of $X$ is given by

$$
f (x) = \frac {1}{\sqrt {2 \pi} \sigma} e ^ {- (x - \mu) ^ {2} / \left(2 \sigma^ {2}\right)}.
$$

![](images/dbef8516a5d35088c39e1b33dc1177989c6a686e6ba9e1ff195e6eb14639e048.jpg)

<details>
<summary>line chart</summary>

| x    | y     |
| ---- | ----- |
| -3.0 | 0.000 |
| -2.5 | 0.047 |
| -2.0 | 0.169 |
| -1.5 | 0.362 |
| -1.0 | 0.540 |
| -0.5 | 0.728 |
| 0.0  | 1.000 |
| 0.5  | 0.728 |
| 1.0  | 0.540 |
| 1.5  | 0.362 |
| 2.0  | 0.169 |
| 2.5  | 0.047 |
| 3.0  | 0.000 |
</details>

\- We'll usually write $X \sim \mathcal{N}(\mu, \sigma^2)$ .

\- Turns out that in practice, many random variable overy the normal distribution

\* Grades  
\* Height of a man or a women

\- Note the following:

\- If $X \sim \mathcal{N}(0,1)$ then

$$
\int_ {- \infty} ^ {\infty} \frac {1}{\sqrt {2 \pi}} e ^ {- x ^ {2} / 2} d x = 1.
$$

\- To show this we use polar coordinates. Let $I = \int_{-\infty}^{\infty} e^{-x^2/2} dx = 2 \int_0^\infty e^{-x^2/2} dx$ The trick is to write

$$
\begin{array}{l} I ^ {2} = 4 \int_ {0} ^ {\infty} \int_ {0} ^ {\infty} e ^ {- x ^ {2} / 2} e ^ {- y ^ {2} / 2} d x y \\ = 4 \int_ {0} ^ {\pi / 2} \int_ {0} ^ {\infty} r e ^ {- r ^ {2} / 2} d r = 4 \cdot \frac {\pi}{2} = 2 \pi , \\ \end{array}
$$

Thus $I = \sqrt{2\pi}$ as needed.

THEOREM 11. To help us compute the mean and variance of $X$ its not too hard to show $X \sim \mathcal{N}\left(\mu, \sigma^2\right)$ if and only if

$$
\frac {X - \mu}{\sigma} = Z \text {   where   } Z \sim \mathcal {N} (0, 1).
$$

PROOF. We only show the ( $\Longleftrightarrow$ ) direction. Note that

$$
\begin{array}{l} F _ {X} (x) = \mathbb {P} (X \leq x) = \mathbb {P} (\sigma Z + \mu \leq x) \\ = \mathbb {P} \left(Z \leq \frac {x - \mu}{\sigma}\right) \\ { = } { F _ { Y } \left( \frac { x - \mu } { \sigma } \right) } \\ \end{array}
$$

for $\sigma > 0$ . Similar for $\sigma < 0$ . By the chain rule

$$
\begin{array}{l} f _ {X} (x) = F _ {X} ^ {\prime} (x) \\ = F _ {Y} ^ {\prime} \left(\frac {x - \mu}{\sigma}\right) \frac {1}{\sigma} \\ = \frac {f _ {Z} \left(\frac {x - \mu}{\sigma}\right)}{\sigma} \\ { = } { \frac { 1 } { \sigma } \frac { 1 } { \sqrt { 2 \pi } } e ^ { - ( x - \mu ) ^ { 2 } / ( 2 \sigma ^ { 2 } ) } . } \\ \end{array}
$$

![](images/200a852e9f1c0e839c0f986a964ffb0886610e3a88cb96ca19bbb8990c4c437f.jpg)

• Summary of the normal distribution:

\- If $X \sim \mathcal{N}\left(\mu, \sigma^2\right)$ then $X$ is normally distributed with

$$
\mathbb {E} X = \mu ,
$$

$$
\operatorname{Var} (X) = \sigma^ {2}.
$$

\- If $X \sim \mathcal{N}(\mu, \sigma^2)$ then $X = \sigma Z + \mu$ where $Z \sim \mathcal{N}(0,1)$ . We call $Z$ a standard normal random variable.

\* A Table of probabilities for Z will be given!!!

\* This will be called a z-score table.

\- Z scores:

\- Because $Z \sim \mathcal{N}(0,1)$ is so important we give it's cumulative distribution function (cdf) a name. The distribution $F_Z(x)$ of $Z$ is

$$
\Phi (x) = \mathbb {P} (Z \leq x) = \frac {1}{\sqrt {2 \pi}} \int_ {- \infty} ^ {x} e ^ {- y ^ {2} / 2} d y.
$$

\- NOTE: A table of $\Phi(x)$ will be given but only for values of $x > 0$

\- Note this is symmetric[ $\underline{DRAW this}$ ] thus here is an important fact: $\Phi(-x)=1-\Phi(x)$

![](images/64fe743adecf18b4e700e521caee6cf1fac6e5cc8a04297483424dc3f759a58b.jpg)

<details>
<summary>area chart</summary>

| Z    | CDF     |
| ---- | ------- |
| 0    | 0       |
| z    | Peak    |
</details>

THEOREM 12. If $X \sim \mathcal{N}(\mu, \sigma)$ then

$$
\mathbb {P} (a <   X <   b) = \mathbb {P} \left(\frac {a - \mu}{\sigma} <   Z <   \frac {b - \mu}{\sigma}\right).
$$

\- Example1: Find $\mathbb{P}(1 \leq X \leq 4)$ if $X \sim \mathcal{N}(2, 25)$ .

\- $\underline{\mathbf{Answer}}$ : Then $\mu = 2$ and $\sigma^2 = 25$ thus $\frac{X - 2}{5} = Z$ so that

$$
\begin{array}{l} \mathbb {P} (1 \leq X \leq 4) = \mathbb {P} \left(\frac {1 - 2}{5} \leq \frac {X - 2}{5} \leq \frac {4 - 2}{5}\right) \\ = \mathbb {P} (-. 2 \leq Z \leq . 4) \\ = \mathbb {P} (X \leq . 4) - \mathbb {P} (X \leq -. 2) \\ = \Phi (. 4) - \Phi (- 2) \\ = . 6 5 5 4 - (1 - \Phi (. 2)) \\ = . 6 5 5 4 - (1 -. 5 7 9 3). \\ \end{array}
$$

\- Example2: Suppose $X$ is normal with mean 6. If $\mathbb{P}(X > 16) = .0228$ , then what is the standard deviation of $X$ ?

\- $\underline{\text{Answer}}$ : We apply our Theorem that says $\frac{X - \mu}{\sigma} = Z$ is $\mathcal{N}(0,1)$ and get

$$
\begin{array}{l} \mathbb {P} (X > 1 6) = . 0 2 2 8 \quad \Longleftrightarrow \quad \mathbb {P} \left(\frac {X - 6}{\sigma} > \frac {1 6 - 6}{\sigma}\right) = . 0 2 2 8 \\ \Longleftrightarrow \quad \mathbb {P} \left(Z > \frac {1 0}{\sigma}\right) = . 0 2 2 8 \\ \Longleftrightarrow \quad 1 - \mathbb {P} \left(Z \leq \frac {1 0}{\sigma}\right) = . 0 2 2 8 \\ \Longleftrightarrow \quad 1 - \Phi \left(\frac {1 0}{\sigma}\right) = . 0 2 2 8 \\ \Longleftrightarrow \quad \Phi \left(\frac {1 0}{\sigma}\right) = . 9 7 7 2. \\ \end{array}
$$

Using the standard normal table we see that $\Phi(2) = .9772$ , thus we must have that

$$
2 = \frac {1 0}{\sigma}
$$

and hence $\sigma = 5$ .

\- Example (Extra): Suppose $X \sim \mathcal{N}(3,9)$ find $\mathbb{P}\left(|X - 3| > 6\right)$ .

\- Answer: Get

$$
\begin{array}{l} \mathbb {P} (| X - 3 | > 6) = \mathbb {P} (X - 3 > 6) + \mathbb {P} (- (X - 3) > 6) \\ = \mathbb {P} (X > 9) + \mathbb {P} (X <   - 3) \\ = \mathbb {P} (Z > 2) + \mathbb {P} (Z <   - 2) \\ = 1 - \Phi (2) + \Phi (- 2) \\ = 2 (1 - \Phi (2)) \\ \approx . 0 4 5 6. \\ \end{array}
$$

• FACT: The 68 - 95 - 99.7 Rule

- About $68\%$ of all area is contained within 1 standard deviation of the mean  
- About $95\%$ of all area is contained within 2 standard deviation of the mean  
- About $99.7\%$ of all area is contained within 3 standard deviation of the mean  
- This can be explained by the following graph:

![](images/1e6b1e4b08c6161b53359d9295bf76cf66512da21abf00a5a7a76887318c6619.jpg)

<details>
<summary>area chart</summary>

| Standard Deviation | Percentage |
| ------------------ | ---------- |
| -3σ                | 0.1%       |
| -2σ                | 2.1%       |
| -1σ                | 13.6%      |
| μ                  | 34.1%      |
| 1σ                 | 34.1%      |
| 2σ                 | 13.6%      |
| 3σ                 | 2.1%       |
| >3σ                | 0.1%       |
</details>

## CHAPTER 9

# Normal approximations to the binomial

## 9.1. The normal approximates Binomial

THEOREM 13. If $S_{n}$ is a binomial with parameter $n$ and $p$ , then

$$
\mathbb {P} \left(a \leq \frac {S _ {n} - n p}{\sqrt {n p (1 - p)}} \leq b\right)\rightarrow \mathbb {P} (a \leq Z \leq b)
$$

as $n\to \infty$ where $Z$ is a $\mathcal{N}(0,1)$ .

\- Recall that if $S_{n} \sim Bin(n,p)$ then its mean is $\mu = np$ and standard deviation is $\sigma = \sqrt{np(1 - p)}$ .  
- So what this theorem says is that if you want to compute $\mathbb{P}(c \leq S_n \leq d)$ then using the fact that

$$
\frac {S _ {n} - n p}{\sqrt {n p (1 - p)}} \approx Z
$$

or

$$
\frac {S _ {n} - \mu}{\sigma} \approx Z,
$$

then

$$
\begin{array}{l} \mathbb {P} \left(c \leq S _ {n} \leq d\right) = \mathbb {P} \left(\frac {c - \mu}{\sigma} \leq \frac {S _ {n} - \mu}{\sigma} \leq \frac {d - \mu}{\sigma}\right) \\ \approx \mathbb {P} \left(\frac {c - \mu}{\sigma} \leq Z \leq \frac {d - \mu}{\sigma}\right). \\ \end{array}
$$

\- Note that $S_{n}$ is really discrete. In fact $S_{n} \in \{0,1,2,\ldots,100\}$ , while the normal distribution is continuous!

\- Note that if I tried to estimate an equality: The wrong way to do it would be:

$$
\begin{array}{l} \mathbb {P} \left(S _ {n} = i\right) = \mathbb {P} \left(\frac {S _ {n} - \mu}{\sigma} = \frac {i - \mu}{\sigma}\right) \\ \approx \mathbb {P} \left(Z = \frac {i - \mu}{\sigma}\right) = 0 \\ \end{array}
$$

as we know that for continuous random variables $X$ we always have $\mathbb{P}(X = a) = 0!$ .

\- Hence we need inequalities if we want to estimate a discrete random variable using a continuous random variable.

\* So we use the following convention. $\mathbb{P}(S_n = i) = \mathbb{P}\left(i - \frac{1}{2} < S_n < i + \frac{1}{2}\right)$ .  
\* We have no problem here, because $S_{n}$ can only be integers, so we're not hurting anything by saying “ $i - \frac{1}{2} < S_{n} < i + \frac{1}{2}$ ” as we know that $S_{n}$ can only be $i$ in that interval anyways.

\- Example: Suppose a fair coin is tossed 100 times.

- (a) What is the probability there will be more than 60 heads?  
- $\underline{\text{Answer:}}$ Let $S_{100} \sim Bin\left(100, \frac{1}{2}\right)$ so that $S_{100}$ represents the numbers of heads in 100 coin tosses.

\* The actual answer would be

$$
\begin{array}{l} \mathbb {P} \left(S _ {1 0 0} > 6 0\right) = \sum_ {i = 6 1} ^ {1 0 0} \mathbb {P} \left(S _ {1 0 0} = i\right) \\ = \sum_ {i = 6 1} ^ {1 0 0} \binom{1 0 0}{i} \left(\frac {1}{2}\right) ^ {i} \left(\frac {1}{2}\right) ^ {1 0 0 - i} \\ = \sum_ {i = 6 1} ^ {1 0 0} \binom{1 0 0}{i} \left(\frac {1}{2}\right) ^ {1 0 0}. \\ \end{array}
$$

\* But this would be almost impossible to do this the long way by hand.  
\* So we will give an approximate answer using the normal distribution:

\- So here take $\mu = np = 50$ and $\sigma = \sqrt{np(1 - p)} = \sqrt{50\frac{1}{2}} = 5$ . We want more than 60, so approximate using $60 + \frac{1}{5}$ :

$$
\begin{array}{l} \mathbb {P} \left(S _ {1 0 0} > 6 0\right) = \mathbb {P} \left(S _ {1 0 0} \geq 6 0. 5\right) = \mathbb {P} \left(\frac {S _ {1 0 0} - 5 0}{5} \geq \frac {6 0 . 5 - 5 0}{5}\right) \\ \approx \mathbb {P} (Z \geq 2. 1) \\ \approx 1 - \Phi (2. 1) \\ = . 0 1 7 9 \\ \end{array}
$$

– (b) Estimate the probability of getting exactly 60 heads?

$$
\begin{array}{l} \mathbb {P} \left(S _ {n} = 6 0\right) = \mathbb {P} \left(5 9. 5 \leq S _ {n} \leq 6 0. 5\right) \\ \approx \mathbb {P} (1. 9 \leq Z \leq 2. 1) \\ \approx \Phi (2. 1) - \Phi (1. 9). \\ \end{array}
$$

## CHAPTER 10

# Some continuous distributions

## 10.1. Exponential Random Variables

\- A continuous R.V. is said to be exponential with parameter $\lambda$ if its pdf is

$$
f (x) = \left\{ \begin{array}{l l} \lambda e ^ {- \lambda x} & \text { if } x \geq 0 \\ 0 & \text { if } x <   0 \end{array} \right.
$$

\- We write $X \sim$ exponential $(\lambda)$ .

\- Summary:

\- CDF: Let $a > 0$ . Note that the cdf is

$$
F _ {X} (a) = \mathbb {P} (X \leq a) = \int_ {0} ^ {a} \lambda e ^ {- \lambda y} d y = - e ^ {\lambda y} | _ {0} ^ {a} = 1 - e ^ {- \lambda a}.
$$

\* Thus

$$
\mathbb {P} (X > a) = 1 - \mathbb {P} (X \leq a) = e ^ {- \lambda a}.
$$

\- Mean: $\mathbb{E}X = \frac{1}{\lambda}$ Thus $\lambda = \frac{1}{\mu}$ .

\- Variance: We have $\operatorname{Var}(X) = \frac{1}{\lambda^2}$ .

\- How to interpret $X$

- $X =$ The amount of time until some specific event occurs.  
- Example:

\* Time until earthquake occurs  
\* Length of a phone call  
\* Time until an accident happens

\- Example1: Suppose that the length of a phone call in minutes is an exponential r.v with average length 10 minutes.

\- Part (a) What's probability of your phone call being more than 10 minutes?

\* Answer: Here $\lambda = \frac{1}{10}$ thus

$$
\mathbb {P} (X > 1 0) = e ^ {- \left(\frac {1}{1 0}\right) 1 0} = e ^ {- 1} \approx . 3 6 8.
$$

\- Part (b) Between 10 and 20 minutes?

\* Answer: We have that

$$
\mathbb {P} (1 0 <   X <   2 0) = F (2 0) - F (1 0) = e ^ {- 1} - e ^ {- 2} \approx . 2 3 3.
$$

- Exponential distribution is Memoryless (Markov)  
- Example2: Suppose the life of an iphone has exponential distribution with mean life of 4 years.

\- Part(a): What is the probability the phone lasts more than 5 years?

\- $\underline{\text{Answer:}}$ Let $X$ denote the life of an iphone (or time until it dies). Note that $X \sim \overline{\text{exponential}}\left(\frac{1}{4}\right)$ since $\lambda = \frac{1}{\mu} = \frac{1}{4}$ . Then

$$
\mathbb {P} (X > 5) = e ^ {- \frac {1}{4} \cdot 5}.
$$

\- Part(b): Given that the iphone has already lasted 3 years, what is the probability that it will last another 5 more years?

\- Answer: We compute

$$
\begin{array}{l} \mathbb {P} (X > 5 + 3 \mid X > 3) = \frac {\mathbb {P} ((X > 8) \cap (X > 3))}{\mathbb {P} (X > 3)} \\ = \frac {\mathbb {P} (X > 8)}{\mathbb {P} (X > 3)} \\ = \frac {e ^ {- \frac {1}{4} \cdot 8}}{e ^ {- \frac {1}{4} \cdot 3}} \\ = e ^ {- \frac {1}{4} \cdot 5}. \\ \end{array}
$$

\- Memoryless: Note that the probability of lasting 5 more years, is the same as if it started 5 years from anew!!

\- In general the memoryless property says that if $t, s > 0$ then

$$
\mathbb {P} (X > t + s \mid X > t) = \mathbb {P} (X > s).
$$

THEOREM 14. If $X$ is an exponential random variable, then $X$ is memoryless.

PROOF. To show this we have

$$
\begin{array}{l} \mathbb {P} (X > t + s \mid X > t) = \frac {\mathbb {P} ((X > t + s) \cap (X > t))}{\mathbb {P} (X > t)} \\ = \frac {\mathbb {P} (X > t + s)}{\mathbb {P} (X > t)} \\ = \frac {e ^ {- \lambda (t + s)}}{e ^ {- \lambda t}} \\ = e ^ {- \lambda s} \\ = \mathbb {P} (X > s), \\ \end{array}
$$

as needed.

![](images/a7a320d4d8322e285795673461440b6b31ca85f35738acc41b6b9f5563f054b2.jpg)

• Example3:(Exam P Q29)

- The $\#$ of days from beginning of a calendar year until accident for a BAD driver is exponentially distributed  
- An insurance company expects $30\%$ of BAD drivers will have an accident during first 50 days.  
- Q: Whats prob that a BAD driver will have Accident during first 80 days?  
- Answer:  
- Step1: Let $X \sim \exp(\lambda)$ number of days until accident. We know

$$
. 3 = \mathbb {P} (X \leq 5 0) = \int_ {0} ^ {5 0} \lambda e ^ {- \lambda x} d x = - e ^ {- \lambda t} | _ {0} ^ {5 0} = 1 - e ^ {- 5 0 \lambda}.
$$

\- Solve for $\lambda$ and get $\lambda = -\frac{1}{50}\ln .7$ .

\- Step2: Then compute

$$
\begin{array}{l} \mathbb {P} (X \leq 8 0) = \int_ {0} ^ {8 0} \lambda e ^ {- \lambda x} d x = - e ^ {- \lambda t} | _ {0} ^ {8 0} = 1 - e ^ {- 8 0 \lambda} \\ = 1 - e ^ {\left(\frac {8 0}{5 0}\right) \ln . 7} = . 4 3 5. \\ \end{array}
$$

## 10.2. Other Continuous Distributions

## • Gamma Distribution:

\- We say $X \sim Gamma(\alpha, \lambda)$ has density

$$
f (x) = \left\{ \begin{array}{l l} \frac {\lambda e ^ {- \lambda x} (\lambda x) ^ {\alpha - 1}}{\Gamma (\alpha)} & x \geq 0 \\ 0 & x <   0 \end{array} \right.
$$

where $\Gamma (\alpha)$ is the Gamma function

$$
\Gamma (\alpha) = \int_ {0} ^ {\infty} e ^ {- y} y ^ {\alpha - 1} d y.
$$

- If $Y \sim Gamma\left(\frac{n}{2}, \frac{1}{2}\right) = \chi_n^2$ , this is called the Chi-Squared distribution.  
- The chi-square distribution is used a lot in statistics.

\* Its mean is $\mathbb{E}X = \frac{\alpha}{\lambda}$ and $\operatorname{Var}X = \frac{\alpha}{\lambda^2}$ .

## • Weibull Distribution:

- Usefull in engineering: Look in the book for its pdf.  
- $X =$ . If there is an object consisting many parts, and suppose that the object experiences death once any of its parts fails. $X =$ lifetime of this object.

## • Cauchy Distribution:

\- We say $X$ is cauchy with parameter $-\infty < \theta < \infty$ if

$$
f (x) = \frac {1}{\pi} \frac {1}{1 + (x - \theta) ^ {2}}.
$$

- Importance: It does not have finite mean: That is $\mathbb{E}X = \infty$ .  
- To see this, We compute for $\theta = 0$

$$
\mathbb {E} X = \frac {1}{\pi} \int_ {- \infty} ^ {\infty} \frac {x}{1 + x ^ {2}} d x
$$

$$
\sim \frac {1}{\pi} \int_ {- \infty} ^ {\infty} \frac {1}{x} d x
$$

$$
\sim \lim _ {x \to \infty} \ln | x | - \ln \lim _ {x \to - \infty} | x |
$$

which is not defined.

## 10.3. The distribution function of a Random variable

\- Fact: For continuous R.V we have the following usefull relationship

\- Since $F(x) = \mathbb{P}(X \leq x) = \int_{-\infty}^{x} f(y) dy$ then by the fundament at theorem of calculus we have

$$
F ^ {\prime} (x) = f (x).
$$

\- Example1: If $X$ is continuous with distribution function $F_{X}$ and density function $f_{X}$ , find a formula for the density function of the random variable $Y = 2X$ .

\- Solution: First you start with the distribution of $Y$ :

\- Step1: First start by writing the cdf of $Y$ and in terms of $F_{X}$ :

$$
F _ {Y} (x) = \mathbb {P} (Y \leq x)
$$

$$
= \mathbb {P} (2 X \leq x)
$$

$$
= \mathbb {P} \left(X \leq \frac {x}{2}\right)
$$

$$
= F _ {X} \left(\frac {x}{2}\right).
$$

\- Step2: Then use the relation $f_{Y}(y) = F_{Y}'(y)$ and take a derivative of both sides to get

$$
F _ {Y} ^ {\prime} (x) = \frac {d}{d x} \left[ F _ {X} \left(\frac {x}{2}\right) \right],
$$

$$
F _ {Y} ^ {\prime} (x) = F _ {X} ^ {\prime} \left(\frac {x}{2}\right) \cdot \left(\frac {x}{2}\right) ^ {\prime}, \text { by   chain   rule   on   RHS }
$$

$$
f _ {Y} (x) = f _ {X} \left(\frac {x}{2}\right) \frac {1}{2}.
$$

\- Goal: To be able to compute the cdf and pdf of $Y = g(X)$ where $g: \mathbb{R} \to \mathbb{R}$ is a function given that we know the cdf and pdf of $X$ .

\- Why is this useful?

\* For example suppose $X$ represent the income for a random US worker. And let $Y = g(X)$ be the amount of taxes a US worker pays per year. Note that taxes $Y$ is dependent on the random variable $X$ . So if we only care about the random variable $Y$ then finding its PDF and CDF can help us find out everything we need to know about $Y$ given we can find the PDF. Recall that any probability and expected value can be found using the pdf.

\- Example2: Let $X \sim \text{Uniform}((0,10))$ and $Y = e^{3X}$ . Find the pdf $f_{Y}$ of $Y$ .

\- Solution: Recall that since $X \sim \text{Uniform}((0,1))$ then

$$
f _ {X} (x) = \left\{ \begin{array}{l l} \frac {1}{1 0} & 0 <   x <   1 0 \\ 0 & \text {otherwise} \end{array} \right..
$$

\- Step1: First start by writing the cdf of $Y$ and in terms of $F_{X}$ :

$$
\begin{array}{l} F _ {Y} (y) = \mathbb {P} (Y \leq y) \\ = \mathbb {P} \left(e ^ {3 X} \leq y\right), \text {then solve for } X \\ = \mathbb {P} (3 X \leq \ln y) \\ = \mathbb {P} \left(X \leq \frac {1}{3} \ln y\right) \\ = F _ {X} \left(\frac {1}{3} \ln y\right). \\ \end{array}
$$

\- Step2: Then use the relation $f_{Y}(y) = F_{Y}'(y)$ and take a derivative

$$
f _ {Y} (y) = F _ {Y} ^ {\prime} (y)
$$

$$
= \frac {d}{d y} \left[ F _ {X} \left(\frac {1}{3} \ln y\right) \right], \text {use chain rule}
$$

$$
= F _ {X} ^ {\prime} \left(\frac {1}{3} \ln y\right) \frac {1}{3 y}
$$

$$
= f _ {X} \left(\frac {1}{3} \ln y\right) \frac {1}{3 y}, \text { since } F _ {X} ^ {\prime} = f _ {X}
$$

$$
= \left\{ \begin{array}{l l} \frac {1}{1 0} \cdot \frac {1}{3 y} & 0 <   \frac {1}{3} \ln y <   1 0 \\ 0 & \text { otherwise } \end{array} \right.
$$

\- but since

$$
0 <   \frac {1}{3} \ln y <   1 0 \quad \Longleftrightarrow \quad 0 <   \ln y <   3 0
$$

$$
\Longleftrightarrow e ^ {0} <   y <   e ^ {3 0}
$$

$$
\Longleftrightarrow 1 <   y <   e ^ {3 0}.
$$

\- then

$$
f _ {Y} (y) = \left\{ \begin{array}{l l} \frac {1}{3 0 y} & 1 <   y <   e ^ {3 0} \\ 0 & \text {otherwise} \end{array} \right..
$$

\- Example3: Let $X \sim \text{Uniform}((0,1])$ and $Y = -\ln X$ . Find the pdf of $Y$ ? What distribution is it?

\- Solution: Recall that

$$
f _ {X} (x) = \left\{ \begin{array}{l l} 1 & 0 <   x <   1 \\ 0 & \text { otherwise } \end{array} \right..
$$

\- Step1: First start with the cdf and write it terms of $F_{X}$

$$
F _ {Y} (x) = \mathbb {P} (Y \leq x)
$$

$$
= \mathbb {P} (- \ln X \leq x)
$$

$$
= \mathbb {P} (\ln X > - x)
$$

$$
= \mathbb {P} (X > e ^ {- x})
$$

$$
= 1 - \mathbb {P} (X \leq e ^ {- x})
$$

$$
= 1 - F _ {X} \left(e ^ {- x}\right).
$$

\- Step2: Then take a derivative

$$
\begin{array}{l} f _ {Y} (x) = F _ {Y} ^ {\prime} (x) \\ = 1 - \frac {d}{d x} F _ {X} \left(e ^ {- x}\right) \\ = - F _ {X} ^ {\prime} \left(e ^ {- x}\right) \cdot \left(- e ^ {- x}\right) \\ = - f _ {X} \left(e ^ {- x}\right) \cdot \left(- e ^ {- x}\right) \\ = f _ {X} \left(e ^ {- x}\right) \cdot e ^ {- x} \\ = \left\{ \begin{array}{l l} 1 \cdot e ^ {- x} & 0 <   e ^ {- x} <   1 \\ 0 & \text { otherwise } \end{array} \right. \\ = \left\{ \begin{array}{l l} e ^ {- x} & - \infty <   - x <   0 \\ 0 & \text { otherwise } \end{array} \right. \\ = \left\{ \begin{array}{l l} e ^ {- x} & 0 <   x <   \infty \\ 0 & \text { otherwise } \end{array} \right. \\ \end{array}
$$

\- Thus $Y \sim$ exponential (1).

\- Example4: Suppose $X$ is uniform on $\left(-\frac{\pi}{2}, \frac{\pi}{2}\right)$ and $Y = \tan X$ . Find the density of $Y$ and what known distribution is it?

\- Solution:

\- Step1: Find the cdf and write in terms of $F_{X}$

$$
\begin{array}{l} F _ {Y} (x) = \mathbb {P} (\tan X \leq x) \\ = \mathbb {P} (X \leq \tan^ {- 1} x) \\ = F _ {X} (\tan^ {- 1} x) \\ \end{array}
$$

\- Step2: Take a derivative and recall that since $\frac{1}{\frac{\pi}{2} + \frac{\pi}{2}} = \frac{1}{\pi}$ then

$$
f _ {X} (x) = \left\{ \begin{array}{l l} \frac {1}{\pi} & - \frac {\pi}{2} <   x <   \frac {\pi}{2} \\ 0 & \text { otherwise. } \end{array} \right.
$$

Thus

$$
\begin{array}{l} f _ {Y} (x) = F _ {Y} ^ {\prime} (x) \\ = \frac {d}{d x} F _ {X} (\tan^ {- 1} x) \\ = F _ {X} ^ {\prime} \left(\tan^ {- 1} x\right) \left(\tan^ {- 1} x\right) ^ {\prime} \\ = F _ {X} ^ {\prime} \left(\tan^ {- 1} x\right) \frac {1}{1 + x ^ {2}} \\ = \left\{ \begin{array}{l l} \frac {1}{\pi} \frac {1}{1 + x ^ {2}} & - \frac {\pi}{2} <   \tan^ {- 1} x <   \frac {\pi}{2} \\ 0 & \text { otherwise. } \end{array} \right. \\ = \left\{ \begin{array}{l l} \frac {1}{\pi} \frac {1}{1 + x ^ {2}} & - \infty <   x <   \infty \\ 0 & \text { otherwise. } \end{array} \right. \\ \end{array}
$$

\- Thus $Y$ is Cauchy(0).

- Exercise: Show that if $Z \sim \mathcal{N}(0,1)$ then $Y = Z^2$ is a Gamma with parameter $\frac{1}{2}$ and $\frac{1}{2}$ .  
- Example5:(Actuarial Exam type question) The time, $T$ , that a manufacturing system is out of operation has cumulative distribution function

$$
F (t) = \left\{ \begin{array}{l l} 1 - \left(\frac {2}{t}\right) ^ {2} & , t > 2 \\ 0 & \text { otherwise } \end{array} \right..
$$

The resulting cost to the company is $Y = T^2$ . Let $f_{Y}$ be the density function for $Y$ . Determine $f_{Y}(y)$ , for $y > 4$ .

\- Answer:

\- Step1: Find the cdf of $Y$ is and

$$
\begin{array}{l} F _ {Y} (y) = \mathbb {P} \left(T ^ {2} \leq y\right) \\ = \mathbb {P} (T \leq \sqrt {y}) \\ = F (\sqrt {y}) \\ = 1 - \frac {4}{y} \\ \end{array}
$$

for $y > 4$ .

\- Step2: Take a derivative

$$
\begin{array}{l} f _ {Y} (y) = F _ {Y} ^ {\prime} (y) \\ = \frac {4}{y ^ {2}}. \\ \end{array}
$$

\- One thing to note, is that we’ve been using the following useful property:

PROPOSITION 15. Suppose $g: \mathbb{R} \to \mathbb{R}$ is a strictly increasing function, then the inverse $g^{-1}$ exists and

$$
g (x) \leq y \text {   implies   } x \leq g ^ {- 1} (y).
$$

## CHAPTER 11

# Multivariate distributions

## 11.1. Joint distribution functions

- We discuss the collection of random variables $(X_{1},\ldots ,X_{n})$ .  
- Discrete:

\- For random variables $X, Y$ we let $p(x, y)$ be the joint probability mass(discrete density) function

$$
p (x, y) = \mathbb {P} (X = x, Y = y).
$$

\* Properties of joint pmf:

· 1) $0 \leq p \leq 1$  
2) $\sum_{i}\sum_{j}p(x_{i},y_{j}) = 1$

\- We also have the multivariate cdf: (★★) defined by

$$
F _ {X, Y} (x, y) = \mathbb {P} (X \leq x, Y \leq y).
$$

\- Example1: Experiment: Suppose you roll two 3-sided die.

- Let $X$ be the largest value obtained on any of the two dice. Let $Y$ be the sum of the two dice. Find the joint pmf of $X$ and $Y$ .  
- Solution: First need to find the values of $X = 1,2,3$ and $Y = 2,3,4,5,6$ .  
- The table for possible outcomes and their associated values $(X,Y)$ :

<table><tr><td>outcome</td><td>1</td><td>2</td><td>3</td></tr><tr><td>1</td><td> $(X = 1, Y = 2) = (1, 2)$ </td><td> $(2, 3)$ </td><td> $(3, 4)$ </td></tr><tr><td>2</td><td> $(2, 3)$ </td><td> $(2, 4)$ </td><td> $(3, 5)$ </td></tr><tr><td>3</td><td> $(3, 4)$ </td><td> $(3, 5)$ </td><td> $(3, 6)$ </td></tr></table>

\- Using this table we have that the p.m.f. is given by:

<table><tr><td>X\Y</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td></tr><tr><td>1</td><td> $\mathbb{P}(X=1,Y=2)=\frac{1}{9}$ </td><td>0</td><td>0</td><td>0</td><td>0</td></tr><tr><td>2</td><td>0</td><td> $\frac{2}{9}$ </td><td> $\frac{1}{9}$ </td><td>0</td><td>0</td></tr><tr><td>3</td><td>0</td><td>0</td><td> $\frac{2}{9}$ </td><td> $\frac{2}{9}$ </td><td> $\frac{1}{9}$ </td></tr></table>

\- Question: Find $\mathbb{P}(X = 2\mid Y = 4)$ ?  
\* Answer: $\mathbb{P}(X = 2\mid Y = 4) = \frac{1 / 9}{3 / 9} = \frac{1}{3}.$

\- Continuous

\- For random variables $X, Y$ we let $f(x, y)$ be the joint probability density function, if

$$
\mathbb {P} (a \leq X \leq b, c \leq Y \leq d) = \int_ {a} ^ {b} \int_ {c} ^ {d} f (x, y) d y d x.
$$

which is equivalent to saying that for any set $D \subset \mathbb{R}^2$ then

$$
\mathbb {P} \left((X, Y) \in D\right) = \int \int_ {D} f (x, y) d A.
$$

\* Properties:

· 1) $f(x, y) \geq 0$  
· 2) $\int_{-\infty}^{\infty}\int_{-\infty}^{\infty}f(x,y)dxdy=1.$

\- We also have the multivariate cdf: (★★) defined by

$$
F _ {X, Y} (x, y) = \mathbb {P} (X \leq x, Y \leq y).
$$

\* Note that $F_{X,Y}(a,b) = \int_{-\infty}^{a}\int_{-\infty}^{b}f(x,y)dydx$ .

\- Thus note that

$$
f (x, y) = \frac {\partial^ {2} F (x , y)}{\partial x \partial y}.
$$

\- Marginal Density: If $f_{X,Y}$ is the joint density of $X, Y$ . We recover the marginal densities of $X, Y$ respectively by the following

$$
f _ {X} (x) = \int_ {- \infty} ^ {\infty} f _ {X, Y} (x, y) d y,
$$

$$
f _ {Y} (y) = \int_ {- \infty} ^ {\infty} f _ {X, Y} (x, y) d x.
$$

• Example2: Let X, Y have joint pdf

$$
f (x, y) = \left\{ \begin{array}{l l} c e ^ {- x} e ^ {- 2 y} & , 0 <   x <   \infty , 0 <   y <   \infty \\ 0 & \text { otherwise } \end{array} \right..
$$

\- $\underline{\mathbf{Part}(\mathbf{a})}$ : Find $c$ that makes this a joint pdf:

\* Sol: Step1: Draw region of Domain first!!!

![](images/071c3f64aeb248121917c187cea61972b8ea8b96e39a431e53bfe0ab1633c1bf.jpg)

<details>
<summary>natural_image</summary>

Blank Cartesian coordinate grid with no plotted data or labels
</details>

\* Thus

$$
\begin{array}{l} 1 = \int_ {0} ^ {\infty} \int_ {0} ^ {\infty} c e ^ {- x} e ^ {- 2 y} d x d y = c \int_ {0} ^ {\infty} e ^ {- 2 y} \left[ - e ^ {- x} \right] _ {x = 0} ^ {x = \infty} d y \\ = c \int_ {0} ^ {\infty} e ^ {- 2 y} d y = c \left[ - \frac {1}{2} e ^ {- 2 y} \right] _ {0} ^ {\infty} = c \frac {1}{2}. \\ \end{array}
$$

Then c = 2.

\- $\underline{\text{Part(b)}}$ : Find $\mathbb{P}(X < Y)$ .

\* Sol: Need to draw the region (Recall Calc III!!) Let $D = \{(x,y) \mid 0 < x < y, 0 < y < \infty\}$

![](images/aacef5faf74b558104e4f0faa7e4fb79374d3d04106ab6ba63e846f5eb6c3982.jpg)

<details>
<summary>area chart</summary>

| x    | y    |
| ---- | ---- |
| 0.0  | 0.0  |
| 0.2  | 0.2  |
| 0.4  | 0.4  |
| 0.6  | 0.6  |
| 0.8  | 0.8  |
| 1.0  | 1.0  |
</details>

- There are two ways to set up this integral:  
- Method1: To set up $dA = dydx$ . We use the Top-Bottom Method:  
- Where the region is bounded by

Top Function: $y = \infty$

Bottom Function $y = x$

Range of Values $0 \leq x \leq \infty$

\- Hence we use this information to set up

$$
\begin{array}{l} \mathbb {P} (X <   Y) = \int \int_ {D} f (x, y) d A \\ = \int_ {0} ^ {\infty} \int_ {x} ^ {\infty} 2 e ^ {- x} e ^ {- 2 y} d y d x \\ = \int_ {0} ^ {\infty} 2 e ^ {- x} \frac {1}{2} \left[ - e ^ {- 2 y} \right] _ {y = x} ^ {y = \infty} d x \\ = \int_ {0} ^ {\infty} e ^ {- x} e ^ {- 2 x} d x = \int_ {0} ^ {\infty} e ^ {- 3 x} x \\ = \frac {1}{3}. \\ \end{array}
$$

\- Method2: To set up $dA = dxdy$ . We use the Right-Left Method:

\- Where the region is bounded by

Right Function: $x = y$

Left Functionx = 0

Range of Values $0 \leq y \leq \infty$

\- Hence we use this information to set up

$$
\begin{array}{l} \mathbb {P} (X <   Y) = \int \int_ {D} f (x, y) d A \\ = \int_ {0} ^ {\infty} \int_ {0} ^ {y} 2 e ^ {- x} e ^ {- 2 y} d x d y \\ = \quad \text { do   some   work } \\ = \frac {1}{3}, \\ \end{array}
$$

which matches the answer from before.

\- $\underline{\mathbf{Part}(\mathbf{c})}$ : Set up $\mathbb{P}(X > 1, Y < 1)$

![](images/b9bbef76d2bbbb33fea36f6544c0e7ff5ef26a71ca7a7984369a1b87a5af6582.jpg)

<details>
<summary>bar chart</summary>

| X | Y |
|---|---|
| 1 | 1 |
</details>

\* The region is given by  
\* Setting this up we have

$$
\mathbb {P} (X > 1, Y <   1) = \int_ {0} ^ {1} \int_ {1} ^ {\infty} 2 e ^ {- x} e ^ {- 2 y} d x d y.
$$

\- $\underline{\mathbf{Part}(\mathbf{d})}$ : Find the marginal $f_{X}(x)$ :

\* Sol:  
\* Then

$$
\begin{array}{l} f _ {X} (x) = \int_ {0} ^ {\infty} f (x, y) d y = \int_ {0} ^ {\infty} 2 e ^ {- x} e ^ {- 2 y} d y \\ = 2 e ^ {- x} \left[ \frac {- 1}{2} e ^ {- 2 y} \right] _ {0} ^ {\infty} = 2 e ^ {- x} \left[ 0 + \frac {1}{2} \right] \\ = e ^ {- x}. \\ \end{array}
$$

\- Part(e): Find $\mathbb{E}X$ We have

$$
\mathbb {E} X = \int_ {0} ^ {\infty} x e ^ {- x} d x = 1
$$

## 11.2. Independent Random Variables

\- Discrete: We say discrete r.v. $X, Y$ are independent if

$$
\mathbb {P} (X = x, Y = y) = \mathbb {P} (X = x) \mathbb {P} (Y = y),
$$

for every $x, y$ in the range of $X$ and $Y$ .

\- This is the same as saying that $X, Y$ are independent if the joint pmf splits into the marginal pmfs: $p_{X,Y}(x,y) = p_X(x) \cdot p_Y(y)$

\- Continuous: We say continuous r.v. $X, Y$ are independent if

$$
\mathbb {P} (X \in A, Y \in B) = \mathbb {P} (X \in A) \mathbb {P} (Y \in B)
$$

for any set $A, B$

\- This equivalent: $\mathbb{P}(X\leq a,Y\leq b) = \mathbb{P}(X\leq a)\mathbb{P}(Y\leq b)$ .

\- Equivalent to $F_{X,Y}(x,y) = F_X(x)F_Y(y)$ .

\- Random variables that are not independent, are said to be dependent.

• How can we check independence?

THEOREM 16. Continuous (discrete) r.v. $X, Y$ are independent if and only if their joint pdf (pmf) can be expressed as

$$
f _ {X, Y} (x, y) = f _ {X} (x) f _ {Y} (y). \text {(Continuous Case)},
$$

$$
p _ {X, Y} (x, y) = p _ {X} (x) p _ {Y} (y) \text {(Discrete Case)}.
$$

PROOF. See textbook.

![](images/a30768ec5d0cef10efe409d93934fd345af55ca29e0ff4aaaf38416b8964f267.jpg)

\- Example1: Let $X, Y$ be r.v. with joint pdf

$$
f (x, y) = 6 e ^ {- 2 x} e ^ {- 3 y} 0 <   x <   \infty , 0 <   y <   \infty .
$$

Are $X, Y$ independent?

\- Solution: Find the marginals $f_{X}$ and $f_{Y}$ and see if $f = f_{X}f_{Y}$ . First

$$
f _ {X} (x) = \int_ {0} ^ {\infty} 6 e ^ {- 2 x} e ^ {- 3 y} d y = 2 e ^ {- 2 x},
$$

$$
f _ {Y} (y) = \int_ {0} ^ {\infty} 6 e ^ {- 2 x} e ^ {- 3 y} d x = 3 e ^ {- 2 y}.
$$

which are both exponential. Since $f = f_{X}f_{y}$ then yes they are independent!

• Example2: Let X, Y have

$$
f _ {X, Y} (x, y) = x + y, 0 <   x <   1, 0 <   y <   1
$$

Are $X, Y$ independent?

\- Solution: Note that there is no way to factor $x + y = f_X(x)f_Y(y)$ , hence they can't be independent.

• Example3: Let X, Y have

$$
f _ {X, Y} (x, y) = 2, 0 <   x <   y <   1
$$

\- Are $X, Y$ independent?

\- Solution:

\* We cannot use the previous argument to claim $f_{X,Y}$ can't split, because for example, maybe hypothetically speaking $2 = 1 \cdot 1$ , so hypothetically it could split.  
\* So we must find the marginal pdfs and then check if $f_{X,Y} = f_X \cdot f_Y$ .

\* Important! But whenever the domain of $f$ is not a rectangle, you MUST draw the region of domain for $f_{X,Y}$ . And here the region is $D = \{(x,y) \mid 0 < x < y < 1\}$ . (Please try drawing this region on your own. If you struggle with this region, go to https://www.wolframalpha.com/ and type in $0 < x < y < 1$ )

\* Note that $f_{X}(x) = \int_{x}^{1} 2dy = 2(1 - x)$ for $0 < x < 1$  
\* Then $f_{Y}(y) = \int_{0}^{y} 2dx = 2y$ for $0 < y < 1$ .  
\* But $f_{X,Y}(x,y) = 2 \neq f_X(x)f_Y(y) = 2(1 - x)2y!!$ Therefore $X,Y$ are NOT independent.

\- Example4: Suppose $X, Y$ are independent uniformly distributed over (0, 1). Find $\mathbb{P}(Y < X)$ .

\- Solution: Since $X, Y$ are independent then using the Theorem form this section we have

$$
f _ {X, Y} (x, y) = f _ {X} (x) f _ {Y} (y) = 1 \cdot 1,
$$

for $0 \leq x \leq 1$ and $0 \leq y \leq 1$ . Draw region ('What do you think probability will be by looking at the region?)

![](images/93acb122105318bb84c87f86bfb3d45f21cba400a8bd06fcc38be8144657aaea.jpg)

<details>
<summary>area chart</summary>

| x | y |
|---|---|
| 0 | 0 |
| 0.2 | 0.2 |
| 0.4 | 0.4 |
| 0.6 | 0.6 |
| 0.8 | 0.8 |
| 1 | 1 |
</details>

\* and get

$$
\begin{array}{l} \mathbb {P} (Y <   X) = \int_ {0} ^ {1} \int_ {0} ^ {x} f (x, y) d y d x \\ = \int_ {0} ^ {1} \int_ {0} ^ {x} 1 d y d x = \int_ {0} ^ {1} x d x \\ = \frac {1}{2}. \\ \end{array}
$$

## 11.3. Sums of independent Random Variables( $\star$ )

\- Fact: If $X, Y$ are independent, its not too hard to show that the cdf of $Z = X + Y$ is

$$
\begin{array}{l} F _ {X + Y} (a) = \mathbb {P} (X + Y \leq a) \\ = \int \int_ {\{x + y \leq a \}} f _ {X} (x) f _ {Y} (y) d x d y \\ = \int_ {- \infty} ^ {\infty} \int_ {- \infty} ^ {a - y} f _ {X} (x) f _ {Y} (y) d x d y \\ = \int_ {- \infty} ^ {\infty} \int_ {- \infty} ^ {a - y} f _ {X} (x) d x f _ {Y} (y) d y \\ = \int_ {- \infty} ^ {\infty} F _ {X} (a - y) f _ {Y} (y) d y. \\ \end{array}
$$

\- By differentiating we have that

$$
f _ {X + Y} (a) = \int_ {- \infty} ^ {\infty} f _ {X} (a - y) f _ {Y} (y) d y.
$$

• (★)Here are some interesting cases:

\- Fact 1(Only thing I'll test you on): If $X_{i} \sim \mathcal{N}\left(\mu_{i}, \sigma_{i}^{2}\right)$ for $1 \leq i \leq n$ and are all independent then $Y = X_{1} + \dots + X_{n} \sim \mathcal{N}\left(\mu_{1} + \dots \mu_{n}, \sigma_{1}^{2} + \dots + \sigma_{n}^{2}\right)$ .

- In particular if $X \sim \mathcal{N}\left(\mu_x, \sigma_x^2\right)$ and $Y \sim \mathcal{N}(\mu_y, \sigma_y^2)$ are independent then $X + Y \sim \mathcal{N}\left(\mu_x + \mu_y, \sigma_x^2 + \sigma_y^2\right)$ and $X - Y \sim \mathcal{N}\left(\mu_x - \mu_y, \sigma_x^2 + \sigma_y^2\right)$ .  
- In general $aX \pm bY \sim \mathcal{N}\left(a\mu_x \pm b\mu_y, a^2\sigma_x^2 + b^2\sigma_y^2\right)$ .

\- Example1: Suppose $T \sim \mathcal{N}(95,25)$ and $H \sim \mathcal{N}(65,36)$ represents the grades of Tyler and Habib. Assume their grades are independent.

\- Part(a): What is the probability that their average grades will be less than 90?

\- Solution: $T + H \sim \mathcal{N}(160, 61)$ . Thus

$$
\begin{array}{l} \mathbb {P} \left(\frac {T + H}{2} \leq 9 0\right) = \mathbb {P} (T + H \leq 1 8 0) \\ = \mathbb {P} \left(Z \leq \frac {1 8 0 - 1 6 0}{\sqrt {6 1}}\right) = \Phi \left(\frac {1 8 0 - 1 6 0}{\sqrt {6 1}}\right) \\ = \Phi (2. 5 6) = . 9 9 6 1 \\ \end{array}
$$

\- Part (b): What is the probability that Habib will have scored higher than Tyler?

\- Solution: Using $H - T \sim \mathcal{N}(-30,61)$ we compute

$$
\begin{array}{l} \mathbb {P} (H > T) = \mathbb {P} (H - T > 0) \\ = 1 - \mathbb {P} (H - T <   0) \\ = 1 - \mathbb {P} \left(Z \leq \frac {0 - (- 3 0)}{\sqrt {6 1}}\right) \\ = 1 - \Phi (3. 8 4) = 1 - 1 = 0. \\ \end{array}
$$

\- Other facts.

\- Fact 2: Let $Z \sim \mathcal{N}(0,1)$ then $Z^2 \sim \chi_1^2$ .
    - If $Z_1, \ldots, Z_n$ are independent $\mathcal{N}(0,1)$ then $Y = Z_1^2 + \cdots + Z_n^2 \sim \chi_n^2$ .

- Fact 3: If $X \sim \text{Poisson}(\lambda)$ and $Y \sim \text{Poisson}(\mu)$ , and they are independent, then $X + Y \sim \overline{\text{Poisson}}(\lambda + \mu)$ .  
- List out stuff and then stop.

## 11.4. Conditional Distributions- Discrete(★)

\- The conditional pmf for a discrete R.V. is

$$
p _ {X \mid Y} (x \mid y) = \mathbb {P} (X = x \mid Y = y)
$$

$$
= \frac {p (x , y)}{p _ {Y} (y)}.
$$

\- We also have the condition cdf: $F_{X|Y}(x \mid y) = \mathbb{P}(X \leq x \mid Y = u)$

\- Fact:

\- If $X, Y$ are independent then

$$
p _ {X | Y} (x \mid y) = p _ {X} (x)
$$

\- Example1: Suppose the joint pmf of $(X,Y)$ is

<table><tr><td>x\y</td><td>0</td><td>1</td></tr><tr><td>0</td><td>.4</td><td>.2</td></tr><tr><td>1</td><td>.1</td><td>.3</td></tr></table>

\- Compute some conditional pmf: Then the second column is

$$
p _ {X | Y} (0 \mid 1) = \frac {. 2}{. 5} = \frac {2}{5} \text { and } p _ {X | Y} (1 \mid 1) = \frac {. 3}{. 5} = \frac {3}{5}.
$$

\- Are they independent? Note that $p_X(0) = .4 + .2 = .6 \neq p_{X|Y}(0 \mid 1)$ , so no!

## 11.5. Conditional Distributions- Continuous( $\star$ )

\- Def: If $X, Y$ are continuous with joint pdf $f(x, y)$ then the conditional pdf of $X$ given $Y = y$ is defined as

$$
f _ {X \mid Y} (x \mid y) = \frac {f (x , y)}{f _ {Y} (y)}.
$$

\- defined only when $f_{Y}(y) > 0$ .

\- Def: The conditional cdf of $X$ given $Y = y$ is

$$
\begin{array}{l} F _ {X \mid Y} (a \mid y) = \mathbb {P} (X \leq a \mid Y = y) \\ = \int_ {- \infty} ^ {a} f _ {X | Y} (x \mid y) d x. \\ \end{array}
$$

• Fact: If X, Y are independent then

$$
f _ {X \mid Y} (x \mid y) = f _ {X} (x).
$$

\- Example1: The joint pdf of $X, Y$ is given by

$$
f x (x, y) = \left\{ \begin{array}{l l} \frac {1 2}{5} x (2 - x - y) & 0 <   x <   1, 0 <   y <   1 \\ 0 & \text {otherwise} \end{array} \right..
$$

Compute the conditional pdf of X give that Y = y where 0 < y < 1.

\- Solution: We have

$$
\begin{array}{l} f _ {X \mid Y} (x \mid y) = \frac {f (x , y)}{f _ {Y} (y)} = \frac {x (2 - x - y)}{\int_ {0} ^ {1} x (2 - x - y) d y} \\ = \frac {x (2 - x - y)}{\frac {2}{3} - \frac {y}{2}}. \\ \end{array}
$$

## 11.6. Joint PDF of functions

## - Goal:

- Recall that from section 5.7 we can find the pdf of a new random variable $Y = g(X)$ .  
- Suppose we know the distributions of $X_{1}, X_{2}$ then what is the distribution of $g_{1}(X_{1}, X_{2})$ and $g_{2}(X_{1}, Y_{1})$  
\* For example if we know $X_{1}, X_{2}$ what is the distribution of $Y_{1} = X_{1} + X_{2}$ and $Y_{2} = X_{1}^{2} - e^{X_{1}X_{2}}$ .

\- Steps to finding the joint cdf of new R.V. made from old ones.:

- Suppose $X_{1}, X_{2}$ are jointly distributed with pdf $f_{X_1, X_2}$ . Let $g_{2}(x_{1}, x_{2}), g_{2}(x_{2}, x_{2})$ be multivariable functions.  
- Goal: Find the joint pdf of $Y_{1} = g_{1}(X_{1}, X_{2})$ and $Y_{2} = g_{1}(X_{2}, X_{2})$  
- Step1: Find the Jacobian:

$$
J \left(x _ {1}, x _ {2}\right) = \left| \begin{array}{c} \nabla g _ {1} \\ \nabla g _ {2} \end{array} \right| = \left| \begin{array}{c c} \frac {\partial g _ {1}}{\partial x _ {1}} & \frac {\partial g _ {1}}{\partial x _ {2}} \\ \frac {\partial g _ {2}}{\partial x _ {1}} & \frac {\partial g _ {2}}{\partial x _ {2}} \end{array} \right| = \frac {\partial g _ {1}}{\partial x _ {1}} \frac {\partial g _ {2}}{\partial x _ {2}} - \frac {\partial g _ {1}}{\partial x _ {2}} \frac {\partial g _ {2}}{\partial x _ {1}} \neq 0.
$$

at all points $(x_{1},x_{2})$

\- Step2: Find the unique solutions of equationf $y_{1} = g_{1}(x_{1}, x_{2})$ and $y_{2} = g_{2}(x_{1}, x_{2})$ in terms of

$$
x _ {1} = h _ {1} \left(y _ {1}, y _ {2}\right),
$$

$$
x _ {2} = h _ {2} \left(y _ {1}, y _ {2}\right).
$$

\- Step3: The joint pdf of $Y_{1}, Y_{2}$ is

$$
\begin{array}{l} f _ {Y _ {1}, Y _ {2}} \left(y _ {1}, y _ {2}\right) = f _ {X _ {1}, X _ {2}} \left(x _ {1}, x _ {2}\right) | J \left(x _ {1}, x _ {2}\right) | ^ {- 1} \\ = f _ {X _ {1}, X _ {2}} \left(h _ {1} \left(y _ {1}, y _ {2}\right), h _ {2} \left(y _ {1}, y _ {2}\right)\right) | J \left(x _ {1}, x _ {2}\right) | ^ {- 1}. \\ \end{array}
$$

\- Example1: Suppose $X_{1}, X_{2}$ have joint distribution

$$
f _ {X _ {1}, X _ {2}} (x _ {1}, x _ {2}) = \left\{ \begin{array}{l l} 2 x _ {1} x _ {2} & 0 \leq x _ {1}, x _ {2} \leq 1 \\ 0 & \text { otherwise } \end{array} \right..
$$

Question: Find the joint pdf of $Y_{1} = X_{1} + X_{2}$ and $Y_{2} = X_{1} - X_{2}$ .

\- Step1: Find the Jacobian: Note that

$$
y _ {1} = g _ {1} \left(x _ {1}, x _ {2}\right) = x _ {1} + x _ {2},
$$

$$
y _ {2} = g _ {2} \left(x _ {1}, x _ {2}\right) = x _ {1} - x _ {2}.
$$

So

$$
J \left(x _ {1}, x _ {2}\right) = \left| \begin{array}{c c} 1 & 1 \\ 1 & - 1 \end{array} \right| = - 2.
$$

\- Step2: Solve for $x_{1}, x_{2}$ and get

$$
x _ {1} = \frac {1}{2} \left(y _ {1} + y _ {2}\right),
$$

$$
x _ {2} = \frac {1}{2} (y _ {1} - y _ {2}).
$$

\- Step3: The joint pdf of $Y_{1}, Y_{2}$ is given by the formula:

$$
\begin{array}{l} f _ {Y _ {1}, Y _ {2}} \left(y _ {1}, y _ {2}\right) = f _ {X _ {1}, X _ {2}} \left(x _ {1}, x _ {2}\right) | J \left(x _ {1}, x _ {2}\right) | ^ {- 1} \\ = f _ {X _ {1}, X _ {2}} \left(\frac {1}{2} \left(y _ {1} + y _ {2}\right), \frac {1}{2} \left(y _ {1} - y _ {2}\right)\right) \frac {1}{| - 2 |} \\ = \left\{ \begin{array}{l l} \frac {1}{2} \left(y _ {1} + y _ {2}\right) \left(y _ {1} - y _ {2}\right) & 0 \leq \frac {1}{2} \left(y _ {1} + y _ {2}\right) \leq 1, \\ & 0 \leq \frac {1}{2} \left(y _ {1} - y _ {2}\right) \leq 1 \\ 0 & \text {otherwise} \end{array} \right. \\ \end{array}
$$

\- Example2: Suppose $X_{1} \sim \mathcal{N}(0,1)$ and $X_{2} \sim \mathcal{N}(0,4)$ and independent.

\- Let $Y_{1} = 2X_{1} + X_{2}$ and $Y_{2} = X_{1} - 3X_{2}$ .

\- Question: Find the joint pdf $f_{Y_1, Y_2}(y_1, y_2)$ of $Y_1$ and $Y_2$ .

\- Step1: Find the Jacobian: Note that

$$
y _ {1} = g _ {1} \left(x _ {1}, x _ {2}\right) = 2 x _ {1} + x _ {2},
$$

$$
y _ {2} = g _ {2} \left(x _ {1}, x _ {2}\right) = x _ {1} - 3 x _ {2}.
$$

So

$$
J \left(x _ {1}, x _ {2}\right) = \left| \begin{array}{c c} 2 & 1 \\ 1 & - 3 \end{array} \right| = - 7.
$$

\- Step2: Solve for $x_{1}, x_{2}$ and get

$$
x _ {1} = \frac {3}{7} y _ {1} + \frac {1}{y} y _ {2}
$$

$$
x _ {2} = \frac {1}{7} y _ {1} - \frac {2}{7} y _ {2}
$$

\- Step3: The joint pdf of $Y_{1}, Y_{2}$ is given by the formula:

$$
\begin{array}{l} f _ {Y _ {1}, Y _ {2}} \left(y _ {1}, y _ {2}\right) = f _ {X _ {1}, X _ {2}} \left(x _ {1}, x _ {2}\right) \left| J \left(x _ {1}, x _ {2}\right) \right| ^ {- 1} \\ = f _ {X _ {1}, X _ {2}} \left(\frac {3}{7} y _ {1} + \frac {1}{y} y _ {2}, \frac {1}{7} y _ {1} - \frac {2}{7} y _ {2}\right) \frac {1}{7}. \\ \end{array}
$$

So we need to find the joint pdf of $X_{1}$ and $X_{2}$ .

\* But since $X_{1} \sim \mathcal{N}(0,1)$ and $X_{2} \sim \mathcal{N}(0,4)$ and independent Then

$$
f _ {X _ {1}} (x _ {1}) = \frac {1}{\sqrt {2 \pi}} e ^ {- x ^ {2} / 2} \text { and } f _ {X _ {2}} (x _ {2}) = \frac {1}{\sqrt {2 \cdot 4 \pi}} e ^ {- x ^ {2} / (2 \cdot 4)}.
$$

Thus by impedance

$$
\begin{array}{l} f _ {X _ {1}, X _ {2}} (x _ {1}, x _ {2}) = f _ {X _ {1}} (x _ {1}) f _ {X _ {2}} (x _ {2}) \\ { = } { \frac { 1 } { \sqrt { 2 \pi } } e ^ { - x ^ { 2 } / 2 } \frac { 1 } { \sqrt { 2 \cdot 4 \pi } } e ^ { - x ^ { 2 } / ( 2 \cdot 4 ) } . } \\ \end{array}
$$

\* Thus we have

$$
f _ {Y _ {1}, Y _ {2}} \left(y _ {1}, y _ {2}\right) = \frac {1}{\sqrt {2 \pi}} e ^ {- \left(\frac {3}{7} y _ {1} + \frac {1}{y} y _ {2}\right) ^ {2} / 2} \frac {1}{\sqrt {8 \pi}} e ^ {- \left(\frac {1}{7} y _ {1} - \frac {2}{7} y _ {2}\right) ^ {2} / 8} \frac {1}{7}.
$$

• Example3(if time): Suppose $X_{1}, X_{2}$ have joint distribution

$$
f _ {X _ {1}, X _ {2}} (x _ {1}, x _ {2}) = \left\{ \begin{array}{l l} x _ {1} + \frac {3}{2} \left(x _ {2}\right) ^ {2} & 0 \leq x _ {1} \leq 1, 0 \leq x _ {2} \leq 1 \\ 0 & \text { otherwise } \end{array} \right..
$$

Question: Find the joint pdf of $Y_{1} = X_{1} + X_{2}$ and $Y_{2} = X_{1}^{2}$ .

\- Step1: Find the Jacobian: Note that

$$
y _ {1} = g _ {1} \left(x _ {1}, x _ {2}\right) = x _ {1} + x _ {2},
$$

$$
y _ {2} = g _ {2} \left(x _ {1}, x _ {2}\right) = x _ {1} ^ {2}.
$$

So

$$
J \left(x _ {1}, x _ {2}\right) = \left| \begin{array}{c c} 1 & 2 \\ 2 x _ {1} & 0 \end{array} \right| = - 4 x _ {2}
$$

\- Step2: Solve for $x_{1}, x_{2}$ and get

$$
x _ {1} = \sqrt {y _ {2}},
$$

$$
x _ {2} = y _ {1} - \sqrt {y _ {2}}.
$$

\- Step3: The joint pdf of $Y_{1}, Y_{2}$ is given by the formula:

$$
\begin{array}{l} f _ {Y _ {1}, Y _ {2}} \left(y _ {1}, y _ {2}\right) = f _ {X _ {1}, X _ {2}} \left(x _ {1}, x _ {2}\right) \left| J \left(x _ {1}, x _ {2}\right) \right| ^ {- 1} \\ = f _ {X _ {1}, X _ {2}} \left(\sqrt {y _ {2}}, y _ {1} - \sqrt {y _ {2}}\right) \frac {1}{| 4 x _ {2} |} \\ = \left\{ \begin{array}{l l} \frac {1}{| 4 x _ {2} |} \left[ \sqrt {y _ {2}} + \frac {3}{2} \left(y _ {1} - \sqrt {y _ {2}}\right) ^ {2} \right] & 0 \leq \sqrt {y _ {2}} \leq 1, \\ & 0 \leq y _ {1} - \sqrt {y _ {2}} \leq 1 \\ 0 & \text {otherwise} \end{array} \right. \\ \end{array}
$$

## CHAPTER 12

## Expectations

## 12.1. Expectation of Sums of R.V.

THEOREM 17. Let $g: \mathbb{R}^2 \to \mathbb{R}$ . If $X, Y$ have joint pmf $p(x, y)$ then

$$
\mathbb {E} \left[ g (X, Y) \right] = \sum_ {y} \sum_ {x} g (x, y) p (x, y).
$$

If $X, Y$ have joint pdf $f(x, y)$ then

$$
\mathbb {E} \left[ g (X, Y) \right] = \int_ {- \infty} ^ {\infty} \int_ {- \infty} ^ {\infty} g (x, y) f (x, y) d x d y.
$$

\- Example1: Suppose the joint p.m.f of $X$ and $Y$ is given by $\begin{array}{c|cc}X\setminus Y & 0 & 2\\ \hline 0 & .2 & .7\\ \hline 1 & 0 & .1 \end{array}$ . Find $\mathbb{E}[XY]$ .

\- Solution: Using the formula we have with the function $g(x,y) = xy$ :

$$
\begin{array}{l} \mathbb {E} [ X Y ] = \sum_ {i, j} x _ {i} y _ {j} p (x _ {i}, y _ {j}) \\ = 0 \cdot 0 p (0, 0) + 1 \cdot 0 p (1, 0) + 0 \cdot 2 p (0, 2) + 1 \cdot 2 p (1, 2) \\ = 0 \cdot 0 \cdot . 2 + 1 \cdot 0 \cdot 0 + 0 \cdot 2 \cdot . 7 + 1 \cdot 2 \cdot . 1 \\ = . 2 \\ \end{array}
$$

\- Example2: Suppose $X, Y$ are independent exponential r.v. with parameter $\lambda = 1$ . Set up a double integral that represents

$$
\mathbb {E} \left[ X ^ {2} Y \right].
$$

\- Solution: Since $X, Y$ are independent then

$$
f _ {X, Y} (x, y) = e ^ {- 1 x} e ^ {- 1 y} = e ^ {- (x + y)}. \quad 0 <   x, y <   \infty .
$$

\- Then DRAW FIRST then

$$
\mathbb {E} \left[ X ^ {2} Y \right] = \int_ {0} ^ {\infty} \int_ {0} ^ {\infty} x ^ {2} y e ^ {- (x + y)} d y d x.
$$

• Example3: Suppose the joint pdf of X, Y is

$$
f (x, y) = \left\{ \begin{array}{l l} 1 0 x y ^ {2} & 0 <   x <   y, 0 <   y <   1 \\ 0 & \text { otherwise } \end{array} \right..
$$

Find $\mathbb{E}XY$ and $\operatorname{Var}(Y)$ .

\- Solution:

\- We First DRAW and then set up

$$
\begin{array}{l} \mathbb {E} X Y = \int_ {0} ^ {1} \int_ {0} ^ {y} x y (1 0 x y ^ {2}) d x d y = 1 0 \int_ {0} ^ {1} \int_ {0} ^ {y} x ^ {2} y ^ {3} d x d y \\ = \frac {1 0}{3} \int_ {0} ^ {1} y ^ {3} y ^ {3} d y = \frac {1 0}{3} \frac {1}{7} = \frac {1 0}{2 1}. \\ \end{array}
$$

\- First note that $\operatorname{Var}(Y) = \mathbb{E}Y^2 - (\mathbb{E}Y)^2$ .

\- Then

$$
\begin{array}{l} \mathbb {E} Y ^ {2} = \int_ {0} ^ {1} \int_ {0} ^ {y} y ^ {2} (1 0 x y ^ {2}) d x d y = 1 0 \int_ {0} ^ {1} \int_ {0} ^ {y} y ^ {4} x d x d y \\ = 5 \int_ {0} ^ {1} y ^ {4} y ^ {2} d y = \frac {5}{7}. \\ \end{array}
$$

and

$$
\begin{array}{l} \mathbb {E} Y = \int_ {0} ^ {1} \int_ {0} ^ {y} y (1 0 x y ^ {2}) d x d y = 1 0 \int_ {0} ^ {1} \int_ {0} ^ {y} y ^ {3} x d x d y \\ = 5 \int_ {0} ^ {1} y ^ {3} y ^ {2} d y = \frac {5}{6}. \\ \end{array}
$$

So that $\operatorname{Var}(Y) = \frac{5}{7} - \left(\frac{5}{6}\right)^2 = \frac{5}{252}$ .

THEOREM 18. (Properties of Expectation)

(a) $\mathbb{E}[X + Y] = \mathbb{E}X + \mathbb{E}Y$  
(b) If $X \leq Y$ then $\mathbb{E}X \leq \mathbb{E}Y$ .

PROOF. Part (a) was proved for the discrete case. So we only need to show the continuous case:

$$
\begin{array}{l} \mathbb {E} \left[ X + Y \right] = \int \int (x + y) f (x, y) d y d x \\ = \int \int x f (x, y) d y d x + \int \int y f (x, y) d y d x \\ = \int x f _ {X} (x) d x + \int y f _ {Y} (y) d y \\ = \mathbb {E} X + \mathbb {E} Y. \\ \end{array}
$$

![](images/4b7f05d06fa0582aa56da7f71b704c3cc3f861ba3822814985957db373b01781.jpg)

\- Example4: Let $X_{1}, \ldots, X_{n}$ be independent and identically distributed random (i.i.d.) random variables. Suppose $\mathbb{E}X_{i} = \mu$ . We call the quantity

$$
\bar {X} = \sum_ {i = 1} ^ {n} \frac {X _ {i}}{n}
$$

the sample mean. Compute $\mathbb{E}\left[\bar{X}\right]$ .

\- Solution: We use the properties of expectation

$$
\begin{array}{l} \mathbb {E} \left[ \bar {X} \right] = \mathbb {E} \left[ \sum_ {i = 1} ^ {n} \frac {X _ {i}}{n} \right] \\ = \frac {1}{n} \mathbb {E} \left[ X _ {1} + \dots + X _ {n} \right] \\ = \frac {1}{n} \left(\mathbb {E} \left[ X _ {1} \right] + \dots + \mathbb {E} \left[ X _ {n} \right]\right) \\ = \frac {1}{n} (\mu + \dots + \mu) = \frac {n \mu}{n} \\ = \mu . \\ \end{array}
$$

\- In statistics, the sample mean is used to estimate the actual mean of a distribution.

THEOREM 19. If $X, Y$ are independent then

$$
\mathbb {E} [ X Y ] = (\mathbb {E} X) (\mathbb {E} Y).
$$

PROOF. In the continuosuc case we have

$$
\begin{array}{l} \mathbb {E} \left[ X Y \right] = \int \int x y f _ {X, Y} (x, y) d y d x \\ = \int \int x y f _ {X} (x) f _ {Y} (y) d y d x \\ = \left(\int x f _ {X} (x) d x\right) \left(\int y f _ {Y} (y) d y\right) \\ = \left(\mathbb {E} X\right) (\mathbb {E} Y). \\ \end{array}
$$

The discrete case is the same, except replace integrals with summations.

\- In general, the following is true:

THEOREM 20. If $X, Y$ are independent and $g, h: \mathbb{R} \to \mathbb{R}$ then

$$
\mathbb {E} \left[ g (X) h (Y) \right] = \mathbb {E} \left[ g (X) \right] \mathbb {E} \left[ h (Y) \right].
$$

## 12.2. Covariance and Correlations.

- Note that $\mathbb{E}X$ and $\operatorname{Var}X$ give information about a single random variable.  
- What statistic can give us information about how $X$ effects $Y$ , or vice versa?

DEFINITION. The covariance between $X$ and $Y$ , is defined by

$$
\operatorname{Cov} (X, Y) = \mathbb {E} \left[ \left(X - \mu_ {X}\right) \left(Y - \mu_ {Y}\right) \right].
$$

• After some algebra one can show that

$$
\operatorname{Cov} (X, Y) = \mathbb {E} [ X Y ] - \mathbb {E} X \mathbb {E} Y.
$$

\- The covariance between two random variables give us information about relationship between the random variables.

- Covariance is a measure of how much two random variables change together.  
- If the greater values of one variable mainly correspond with the greater values of the other variable, and the same holds for the lesser values, i.e., the variables tend to show similar behavior, the covariance is positive.  
\* Thus covariance measures if there is a linear relationship between $X$ and $Y$ .  
- The sign of the covariance therefore shows the tendency in the linear relationship between the variables.  
- For example, the following plots shows a positive linear relationship between $X$ and $Y$ :

![](images/64908a431074cbe7cf4daf03811bccb1153513c8a97bfecd51f33c0910385b5e.jpg)

<details>
<summary>scatterplot</summary>

| x    | y    |
| ---- | ---- |
| -20  | 3    |
| -15  | 4    |
| -10  | 5    |
| -5   | 6    |
| 0    | 7    |
| 5    | 8    |
| 10   | 9    |
| 15   | 10   |
| 20   | 11   |
| 25   | 12   |
| 30   | 13   |
| 35   | 14   |
| 40   | 15   |
| 45   | 16   |
| 50   | 17   |
| 55   | 18   |
| 60   | 19   |
</details>

In this case $\operatorname{Cov}(X,Y) > 0$ .

\- Note: If $X, Y$ are independent then $\operatorname{Cov}(X, Y) = 0$ . (This is not true in the other direction. Meaning $\operatorname{Cov}(X, Y) = 0$ does not imply that $X, Y$ are independent!)
- So $\operatorname{Cov}(X, Y) = 0$ means they are uncorrelated.

\- Properties:

- (i) $\operatorname{Cov}(X,Y) = \operatorname{Cov}(Y,X)$  
- (ii) $\operatorname{Cov}(X, X) = \operatorname{Var}(X)$  
- (iii) $\operatorname{Cov}(aX,Y) = a\operatorname{Cov}(X,Y)$  
- (iv) $\operatorname{Cov}\left(\sum_{i} X_{i}, \sum_{j} Y_{j}\right) = \sum_{i} \sum_{j} \operatorname{Cov}(X_{i}, Y_{j})$ .

THEOREM 21. (★) Formula for Sum of Variation:

$$
\operatorname{Var} (X + Y) = \operatorname{Var} (X) + \operatorname{Var} (Y) + 2 \operatorname{Cov} (X, Y).
$$

Gives us a formula for variation of $X_{1},\ldots ,X_{n}$ :

$$
\operatorname{Var} \left(\sum_ {i = 1} ^ {n} X _ {i}\right) = \sum_ {i = 1} ^ {n} \operatorname{Var} \left(X _ {i}\right) + 2 \sum \sum_ {i <   j} \operatorname{Cov} \left(X _ {i}, X _ {j}\right).
$$

• Fact: Note that if X, Y are independent then

$$
\operatorname{Var} (X + Y) = \operatorname{Var} (X) + \operatorname{Var} (Y).
$$

\- Finally we have the following: Its standardized way to know how correlated two random variables are:

DEFINITION. The correlation coefficient of two random variables $X$ and $Y$ , denoted by $\rho(X, Y)$ is defined by

$$
\rho (X, Y) = \frac {\operatorname{Cov} (X , Y)}{\sqrt {\operatorname{Var} (X) \operatorname{Var} (Y)}}.
$$

\- Fact:

$-(1) - 1 \leq \rho(X, Y) \leq 1$  
- (2) If $\rho(X,Y)=1$ then $Y=a+bX$ where $b=\frac{\sigma_y}{\sigma_x}>0$ (Straight positive sloped line)  
- (3) If $\rho(X,Y) = -1$ then $Y = a + bX$ where $b = -\frac{\sigma_y}{\sigma_x} < 0$ (Straight negatively sloped line)  
- (4) This $\rho$ is a measure of linearity between $Y$ and $X$ .

\* $\rho > 0$ positive linearity: Meaning that if you were to draw a line of best fit, then it must be a positive sloped line

\- The closer $\rho$ gets to 1, the more $(X,Y)$ seems to be in a positive sloped straight line

\* $\rho < 0$ negative linearity: Meaning that if you were to draw a line of best fit, then it must be a negative sloped line

\- The closer $\rho$ gets to $-1$ , the more $(X,Y)$ seems to be in a negative sloped straight line

\- (5) If $\rho(X,Y)=0$ , then $X$ and $Y$ are uncorrelated.

\- Warning:

$-\rho (X,Y)$ does not pick up any other relationship, such as quadratic, or cubic

\- $\rho(X, Y)$ is not the slope of the line of best fit. It is simply tell us if it's positive, or negative relationship, and the strength of relationship.

\- Example1: Suppose $X, Y$ are random variables whose joint pdf is given by

$$
f (x, y) = \left\{ \begin{array}{l l} \frac {1}{y} & 0 <   y <   1, 0 <   x <   y \\ 0 & \text { otherwise } \end{array} \right..
$$

- Part (a): Find the covariance of $X$ and $Y$ .  
- Part (b) Compute $\operatorname{Var}(X)$ and $\operatorname{Var}(Y)$ .  
- $\underline{\text{Part (c)}}$ Calculate $\rho(X,Y)$ .  
- Solution:  
- $\underline{\text{Part (a):}}$ Find the covariance of $X$ and $Y$ .

\- Recall that $\operatorname{Cov}(X,Y) = \mathbb{E}XY - \mathbb{E}X\mathbb{E}Y$ . So

$$
\mathbb {E} X Y = \int_ {0} ^ {1} \int_ {0} ^ {y} x y \frac {1}{y} d x d y = \int_ {0} ^ {1} \frac {y ^ {2}}{2} d y = \frac {1}{6}
$$

$$
\mathbb {E} X = \int_ {0} ^ {1} \int_ {0} ^ {y} x \frac {1}{y} d x d y = \int_ {0} ^ {1} \frac {y}{2} d y = \frac {1}{4}.
$$

$$
\mathbb {E} Y = \int_ {0} ^ {1} \int_ {0} ^ {y} y \frac {1}{y} d x d y = \int_ {0} ^ {1} y d y = \frac {1}{2}.
$$

Thus

$$
\operatorname{Cov} (X, Y) = \mathbb {E} X Y - \mathbb {E} X \mathbb {E} Y
$$

$$
= \frac {1}{6} - \frac {1}{4} \frac {1}{2}
$$

$$
= \frac {1}{2 4}.
$$

\- Part (b): Compute $\operatorname{Var}(X)$ and $\operatorname{Var}(Y)$ .

\- We have that

$$
\mathbb {E} X ^ {2} = \int_ {0} ^ {1} \int_ {0} ^ {y} x ^ {2} \frac {1}{y} d x d y = \int_ {0} ^ {1} \frac {y ^ {2}}{3} d y = \frac {1}{9}.
$$

$$
\mathbb {E} Y ^ {2} = \int_ {0} ^ {1} \int_ {0} ^ {y} y ^ {2} \frac {1}{y} d x d y = \int_ {0} ^ {1} y ^ {2} d y = \frac {1}{3}.
$$

\- Thus recall that

$$
\operatorname{Var} (X) = \mathbb {E} X ^ {2} - (\mathbb {E} X) ^ {2}
$$

$$
= \frac {1}{9} - \left(\frac {1}{4}\right) ^ {2} = \frac {7}{1 4 4}
$$

Also

$$
\operatorname{Var} (Y) = \mathbb {E} Y ^ {2} - (\mathbb {E} Y) ^ {2}
$$

$$
= \frac {1}{3} - \left(\frac {1}{2}\right) ^ {2} = \frac {1}{1 2}.
$$

\- Part (c): Calculate $\rho(X,Y)$ .

\- We now use

$$
\rho (X, Y) = \frac {\operatorname{Cov} (X , Y)}{\sqrt {\operatorname{Var} (X) \operatorname{Var} (Y)}}
$$

$$
= \frac {\frac {1}{2 4}}{\sqrt {\left(\frac {7}{1 4 4}\right) \left(\frac {1}{1 2}\right)}} \approx . 6 5 4 7.
$$

## CHAPTER 13

## Moment generating functions

## 13.1. Moment Generating Functions

\- For each random variable $X$ , we can define its moment generating function $m_X(t)$ by

$$
\begin{array}{l} {m _ {X} (t)} = {\mathbb {E} \left[ e ^ {t X} \right]} \\ = \left\{ \begin{array}{l l} \sum_ {x _ {i}} e ^ {t x _ {i}} p (x _ {i}) & , \text {if X is discrete} \\ \int_ {- \infty} ^ {\infty} e ^ {t x} f (x) d s & , \text {if X is continuous} \end{array} \right.. \\ \end{array}
$$

\- $m_X(t)$ is called the moment generating function (m.g.f.) because we can find all the moments of $X$ by differentiating $m(t)$ and then evaluating at $t = 0$ .

\- Note that

$$
\begin{array}{l} {m ^ {\prime} (t)} = {\frac {d}{d t} \mathbb {E} \left[ e ^ {t X} \right]} \\ = \mathbb {E} \left[ \frac {d}{d t} e ^ {t X} \right] \\ = \mathbb {E} \left[ X e ^ {t X} \right]. \\ \end{array}
$$

Now evaluate at t = 0 and get

$$
m ^ {\prime} (0) = \mathbb {E} \left[ X e ^ {0 \cdot X} \right] = \mathbb {E} [ X ].
$$

\- Similarly,

$$
\begin{array}{l} {m ^ {\prime \prime} (t)} = {\frac {d}{d t} \mathbb {E} \left[ X e ^ {t X} \right]} \\ = \mathbb {E} \left[ X ^ {2} e ^ {t X} \right] \\ \end{array}
$$

so that

$$
m ^ {\prime \prime} (0) = \mathbb {E} \left[ X ^ {2} e ^ {0} \right] = \mathbb {E} \left[ X ^ {2} \right].
$$

THEOREM 22. For all $n \geq 0$ we have

$$
\mathbb {E} \left[ X ^ {n} \right] = m ^ {(n)} (0).
$$

• Examples of Moment generating Functions  
- Bernoulli: Recall that $p(1) = p$ and $p(0) = 1 - p$ . Thus

$$
\begin{array}{l} m _ {X} (t) = \mathbb {E} e ^ {t X} = e ^ {t \cdot 0} p (0) + e ^ {t \cdot 1} p (1) \\ = p e ^ {t} + (1 - p). \\ \end{array}
$$

\- Binomial: Recall that $X \sim \text{Bin}(n, p)$ if $X = \sum_{i=0}^{n} Y_i$ where $Y_i \sim \text{Bern}(p)$ thus

$$
\begin{array}{l} m _ {X} (t) = \mathbb {E} e ^ {t X} = \mathbb {E} e ^ {t X \sum_ {i = 0} ^ {n} Y _ {i}} \\ = \mathbb {E} \left[ \left(e ^ {t Y _ {1}}\right) \dots \left(e ^ {t Y _ {n}}\right) \right] \\ = \mathbb {E} \left[ e ^ {t Y _ {1}} \right] \dots \mathbb {E} \left[ e ^ {t Y _ {n}} \right], \text {by independence} \\ = \left(p e ^ {t} + (1 - p)\right) ^ {n} \\ \end{array}
$$

\- Poisson: If $X \sim \text{Poisson}(\lambda)$ then

$$
\begin{array}{l} m _ {X} (t) = \mathbb {E} e ^ {t X} = \sum_ {n = 0} ^ {\infty} e ^ {t n} e ^ {- \lambda} \frac {\lambda^ {n}}{n !} \\ = e ^ {- \lambda} \sum_ {n = 0} ^ {\infty} e ^ {t n} \frac {\lambda^ {n}}{n !} \\ = e ^ {- \lambda} \sum_ {n = 0} ^ {\infty} \frac {(e ^ {t} \lambda) ^ {n}}{n !} \\ \end{array}
$$

now recall from Calculus 2 that $e^x = \sum_{n=0}^{\infty} \frac{x^n}{n!}$ so that

$$
\begin{array}{l} m _ {X} (t) = e ^ {- \lambda} \sum_ {n = 0} ^ {\infty} \frac {x ^ {n}}{n !}, \text { with } x = e ^ {t} \lambda \\ = e ^ {- \lambda} e ^ {e ^ {t} \lambda} \\ = e ^ {e ^ {t} \lambda - \lambda} \\ = \exp (\lambda (e ^ {t} - 1)) \\ \end{array}
$$

\- Exponential: If $X \sim \exp(\lambda)$ then

$$
\begin{array}{l} m _ {X} (t) = \mathbb {E} e ^ {t X} \\ = \int_ {0} ^ {\infty} e ^ {t x} \lambda e ^ {- \lambda x} d x \\ = \frac {\lambda}{\lambda - t}, \\ \end{array}
$$

which is valued whenever $t > \lambda$ .

\- Standard Normal: If $X \sim \mathcal{N}(0,1)$ then

$$
\begin{array}{l} m _ {X} (t) = \mathbb {E} e ^ {t X} = \frac {1}{\sqrt {2 \pi}} \int_ {- \infty} ^ {\infty} e ^ {t x} e ^ {- x ^ {2} / 2} \\ = e ^ {t ^ {2} / 2}. \\ \end{array}
$$

\- Normal: If $X \sim \mathcal{N}(\mu, \sigma^2)$ then $X = \mu + \sigma Z$ so that

$$
\begin{array}{l} m _ {X} (t) = \mathbb {E} e ^ {t X} \\ = \mathbb {E} e ^ {t \mu} e ^ {t \sigma Z} = e ^ {t \mu} \mathbb {E} e ^ {(t \sigma) Z} \\ = e ^ {t \mu} m _ {X} (t \sigma) = e ^ {t \mu} e ^ {(t \sigma) ^ {2} / 2} \\ = \exp \left(t \mu + \frac {t ^ {2} \sigma^ {2}}{2}\right). \\ \end{array}
$$

\- Property: Suppose $X, Y$ are independent then what is that m.g.f. of $X + Y$ ?

\- Let's try to figure out:

$$
\begin{array}{l} m _ {X + Y} (t) = \mathbb {E} e ^ {t (X + Y)} = \mathbb {E} \left(e ^ {t X} e ^ {t Y}\right) \\ = \mathbb {E} \left(e ^ {t X}\right) \mathbb {E} \left(e ^ {t Y}\right), \text {   by   independence } \\ = m _ {X} (t) m _ {Y} (t). \\ \end{array}
$$

\- Thus we know that

$$
m _ {X + Y} (t) = m _ {X} (t) m _ {Y} (t).
$$

\- Note: Also note that of $f_{X}(x)$ is the pdf of a r.v. then it's m.g.f is

$$
\mathbb {E} e ^ {t X} = \int e ^ {t x} f _ {X} (x) d x.
$$

This is similar to the $\underline{\text{laplace transform}}$ of $f_X(x)$ . $[\mathcal{L}[f](s) = \int e^{-sx} f_X(x) dx]$ .

\- Recall that there is one to one correspondence of laplace transforms. That completely determines a function.

THEOREM 23. If $m_X(t) = m_Y(t) < \infty$ for all $t$ in an interval, then $X$ and $Y$ have the same distribution. That is, m.g.f's completely determines the distribution.

\- Example1: Suppose that m.g.f of $X$ is given by $m(t) = e^{3(e^t - 1)}$ . Find $\mathbb{P}(X = 0)$ .

\- Solution: (We want to work backwards). Match this m.g.f to a known m.g.f in our table. Looks like

$$
m (t) = e ^ {3 \left(e ^ {t} - 1\right)} = e ^ {\lambda \left(e ^ {t} - 1\right)} \quad \text { where } \lambda = 3.
$$

Thus $X\sim \text{Poisson}(3)$ . Thus

$$
\mathbb {P} (X = 0) = e ^ {- \lambda} \frac {\lambda^ {0}}{0 !} = e ^ {- 3}.
$$

\- Summary:

(1) $m(t) = \mathbb{E}e^{tX}$ . We have a table of mgf of distributions:  
(2) The m.g.f helps us find moments: $\mathbb{E}[X^n] = m^{(n)}(0)$  
(3) If $X, Y$ are independent then $m_{X + Y}(t) = m_X(t)m_Y(t)$ .  
(4) The m.g.f. helps us determine the distribution of random variables. If $m_X(t) = m_Y(t)$ then $X$ and $Y$ have the same distribution.

\- Recall we had a section on sums of independent random variables.

\- Example2: Recall $X \sim \mathcal{N}\left(\mu_x, \sigma_x^2\right)$ and $Y \sim \mathcal{N}(\mu_y, \sigma_y^2)$ , independent. Then what is

$$
X + Y \sim \mathcal {N} (\mathrm{?},?)
$$

\- Sol: Note that

$$
\begin{array}{l} m _ {X + Y} (t) = m _ {X} (t) m _ {Y} (t) \\ = \exp \left(t \mu_ {x} + \frac {t ^ {2} \sigma_ {x} ^ {2}}{2}\right) \exp \left(t \mu_ {y} + \frac {t ^ {2} \sigma_ {y} ^ {2}}{2}\right) \\ = \exp \left(t \left(\mu_ {x} + \mu_ {y}\right) + \frac {t ^ {2} \left(\sigma_ {x} ^ {2} + \sigma_ {y} ^ {2}\right)}{2}\right). \\ \end{array}
$$

So then you look at our table and check which distribution has this mg.f. with $\mu = \mu_x + \mu_y$ and $\sigma^2 = \sigma_x^2 +\sigma_y^2$ . so that $X + Y\sim \mathcal{N}\left(\mu_x + \mu_y,\sigma_x^2 +\sigma_y^2\right)$

\- Example3: Suppose $X \sim \text{bin}(n, p)$ and $Y \sim \text{bin}(m, p)$ , independent, then what is the distribution of $X + Y$ ?

\- Solution: We use

$$
\begin{array}{l} {m _ {X + Y} (t)} = {m _ {X} (t) m _ {Y} (t)} \\ = \left(p e ^ {t} + (1 - p)\right) ^ {n} \left(p e ^ {t} + (1 - p)\right) ^ {m} \\ = \left(p e ^ {t} + (1 - p)\right) ^ {n + m}. \\ \end{array}
$$

Look at the table and see what distribution has this m.g.f. Thus

$$
X + Y \sim b i n (n + m, p).
$$

\- Example4: Suppose $X$ is a discrete random variable and has the m.g.f.

$$
m _ {X} (t) = \frac {1}{7} e ^ {2 t} + \frac {3}{7} e ^ {3 t} + \frac {2}{7} e ^ {5 t} + \frac {1}{7} e ^ {8 t}.
$$

Question: What is the p.m.f of $X$ ? Find $\mathbb{E}X$ .

\- Solution(a): This doesn't match any of the known mg.f.s. Thus we can read off from the mgf that since

$$
\frac {1}{7} e ^ {2 t} + \frac {3}{7} e ^ {3 t} + \frac {2}{7} e ^ {5 t} + \frac {1}{7} e ^ {8 t} = \sum_ {i = 1} ^ {4} e ^ {t x _ {i}} p (x _ {i})
$$

then $p(2) = \frac{1}{7}, p(3) = \frac{3}{7}, p(5) = \frac{2}{7}$ and $p(8) = \frac{1}{7}$ .

\- Solution(b): First

$$
m ^ {\prime} (t) = \frac {2}{7} e ^ {2 t} + \frac {9}{7} e ^ {3 t} + \frac {1 0}{7} e ^ {5 t} + \frac {8}{7} e ^ {8 t},
$$

so that

$$
\mathbb {E} [ X ] = m ^ {\prime} (0) = \frac {2}{7} + \frac {9}{7} + \frac {1 0}{7} + \frac {8}{7} = \frac {2 9}{7}.
$$

• Example5: Suppose X has m.g.f

$$
m _ {X} (t) = (1 - 2 t) ^ {- \frac {1}{2}} \text {   for   } t <   \frac {1}{2}.
$$

Find the first and second moments of $X$ .

\- Solution: We have

$$
m _ {X} ^ {\prime} (t) = - \frac {1}{2} (1 - 2 t) ^ {- \frac {3}{2}} (- 2) = (1 - 2 t) ^ {- \frac {3}{2}},
$$

$$
m _ {X} ^ {\prime \prime} (t) = - \frac {3}{2} (1 - 2 t) ^ {- \frac {5}{2}} (- 2) = 3 (1 - 2 t) ^ {- \frac {5}{2}}.
$$

So that

$$
\begin{array}{l} \mathbb {E} X = m _ {X} ^ {\prime} (0) = (1 - 2 \cdot 0) ^ {- \frac {3}{2}} = 1, \\ \mathbb {E} X ^ {2} = m _ {X} ^ {\prime \prime} (0) = 3 (1 - 2 \cdot 0) ^ {- \frac {5}{2}} = 3. \\ \end{array}
$$

## CHAPTER 14

## Limit Laws

## 14.1. The Central Limit Theorem

\- The CLT is one of the most remarkable theorems in Probability.

\- It helps us understand why the empirical frequencies of so many natural populations exhibit bell-shaped (normal) curves.

\- Recall that $i.i.d.$ means independent and identically distributed random variables.

THEOREM 24. (CLT) Let $X_{1}, X_{2}, X_{3} \ldots$ be i.i.d. each with mean $\mu$ and variance $\sigma^{2}$ . Then the distribution of

$$
\frac {X _ {1} + \cdots + X _ {n} - n \mu}{\sigma \sqrt {n}}
$$

tends to the standard normal $Z$ as $n\to \infty$ . That is,

$$
\mathbb {P} \left(\frac {X _ {1} + \cdots + X _ {n} - n \mu}{\sigma \sqrt {n}} \leq b\right) \approx \mathbb {P} (Z \leq b) = \Phi (b).
$$

when $n$ is large.

\- The CLT helps us approximate the probability of anything involving $X_{1} + \cdots + X_{n}$ where $X_{i}$ are independent and identically distributed.

\- When approximating discrete distributions: USE the $\pm .5$ continuity correction:

\- Example1: If 10 fair dice are rolled, find the approximate probability that the sum obtained is between 30 and 40, inclusive.

\- Solution: Let $X_{i}$ denote the value of the ith die. Recall that

$$
\mathbb {E} \left(X _ {i}\right) = \frac {7}{2} \operatorname{Var} (X _ {i}) = \frac {3 5}{1 2}.
$$

Take

$$
X = X _ {1} + \dots + X _ {n}
$$

to be their sum.

\- Using the CLT we need

$$
n \mu = 1 0 \cdot \frac {7}{2} = 3 5
$$

$$
\sigma \sqrt {n} = \sqrt {\frac {3 5 0}{1 2}}
$$

thus using the continuity correction, then

$$
\mathbb {P} (2 9. 5 \leq X \leq 4 0. 5) = \mathbb {P} \left(\frac {2 9 . 5 - 3 5}{\sqrt {\frac {3 5 0}{1 2}}} \leq \frac {X - 3 5}{\sqrt {\frac {3 5 0}{1 2}}} \leq \frac {4 0 . 5 - 3 5}{\sqrt {\frac {3 5 0}{1 2}}}\right)
$$

$$
\approx \mathbb {P} (- 1. 0 1 8 4 \leq Z \leq 1. 0 1 8 4)
$$

$$
= \Phi (1. 0 1 8 4) - \Phi (- 1. 0 1 8 4)
$$

$$
= 2 \Phi (1. 0 1 8 4) - 1 = . 6 9 2.
$$

\- Example2: An instructor has 1000 exams that will be graded in sequence.

- The times required to grade exam exam are i.i.d. with $\mu = 20$ minutes and SD $\sigma = 4$ minutes.  
- Approximate prob that the instructor will grade at least 25 exams in the first 450 minutes of work.  
- Solution:  
- Let $X_{i}$ be the time it takes to grade exam $i$ . Then

$$
X = X _ {1} + \dots + X _ {2 5}
$$

is the time it takes to grade the first 25 exams. We want $\mathbb{P}(X\leq 450)$ .

\- Use CLT,

$$
n \mu = 2 5 \cdot 2 0 = 5 0 0
$$

$$
\sigma \sqrt {n} = 4 \sqrt {2 5} = 2 0.
$$

\- Thus

$$
\begin{array}{l} \mathbb {P} (X \leq 4 5 0) = \mathbb {P} \left(\frac {X - 5 0 0}{2 0} \leq \frac {4 5 0 - 5 0 0}{2 0}\right) \\ \approx \mathbb {P} (Z \leq - 2. 5) \\ = 1 - \Phi (2. 5) \\ = . 0 0 6. \\ \end{array}
$$