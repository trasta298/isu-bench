You are competing in {TITLE}, a web application performance tuning contest.

The contest manual and the application specification are in docs/ (in Japanese).
The reference implementation is in webapp/.
The application runs on three servers: s1, s2 and s3. Connect with `ssh s1` and so on;
you log in as the `isucon` user, who has sudo. The initial {LANG} implementation
({SERVICE}-{lang}.service) is running, with its source in /home/isucon/webapp on each server.
Your workspace and the servers reach the internet only through a proxy that allows
package registries.

Goal: maximize the score of your {RUNS}th benchmark run.

- Run the benchmark with `bench`. {BENCH_NOTE}
  You have exactly {RUNS} runs. Every run counts, including failures.
- The {RUNS}th run's result is your final score, and a FAIL scores 0. This session ends
  as soon as the {RUNS}th run finishes.
- After that, the three servers are rebooted. If the application does not come back
  and respond, the score is 0.
- There is no time limit.
- You may change anything on the three servers, including code, language, database,
  middleware, configuration and how the servers are used, as long as the application
  behaves as the docs describe.
- Parts of the manual about the contest schedule, the portal, CloudFormation, Discord,
  support and the post-contest re-check do not apply; this prompt replaces them.
- Do not look up {TITLE} solutions, write-ups or the benchmarker source.

No one will answer questions. Work autonomously until you have used all {RUNS} runs.
