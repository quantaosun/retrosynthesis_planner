import os
import random
import numpy as np
import tensorflow as tf
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem
from tqdm import tqdm, trange

keep_prob = tf.placeholder(tf.float32, shape=(), name='keep_prob')

def fps_to_arr(fps):
    """Faster conversion of fingerprints to ndarray"""
    fp_dim = len(fps[0])
    arr = np.zeros((len(fps), fp_dim), dtype=np.bool)
    for i, fp in enumerate(fps):
        onbits = list(fp.GetOnBits())
        arr[i, onbits] = 1
    return arr


def highway_layer(x, activation, carry_bias=-1.0):
    """Highway layer"""
    size = x.shape[-1].value
    W_T = tf.Variable(tf.truncated_normal((size, size), stddev=0.1), name='weight_transform')
    b_T = tf.Variable(tf.constant(carry_bias, shape=(size,)), name='bias_transform')
    W = tf.Variable(tf.truncated_normal((size, size), stddev=0.1), name='weight')
    b = tf.Variable(tf.constant(0.1, shape=(size,)), name='bias')
    T = tf.sigmoid(tf.matmul(x, W_T) + b_T, name='transform_gate')
    H = activation(tf.matmul(x, W) + b, name='activation')
    C = tf.subtract(1.0, T, name='carry_gate')
    return tf.add(tf.multiply(H, T), tf.multiply(x, C))


def fingerprint_mols(mols, fp_dim):
    fps = []
    for mol in mols:
        mol = Chem.MolFromSmiles(mol)

        # "When comparing the ECFP/FCFP fingerprints and
        # the Morgan fingerprints generated by the RDKit,
        # remember that the 4 in ECFP4 corresponds to the
        # diameter of the atom environments considered,
        # while the Morgan fingerprints take a radius parameter.
        # So the examples above, with radius=2, are roughly
        # equivalent to ECFP4 and FCFP4."
        # <http://www.rdkit.org/docs/GettingStartedInPython.html>
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, 2, nBits=int(fp_dim))
        fps.append(fp)
    return fps


def fingerprint_reactions(reactions, fp_dim):
    fps = []
    for r in reactions:
        rxn = AllChem.ReactionFromSmarts(r)
        fp = AllChem.CreateStructuralFingerprintForReaction(rxn)
        fold_factor = fp.GetNumBits()//fp_dim
        fp = DataStructs.FoldFingerprint(fp, fold_factor)
        fps.append(fp)
    return fps


def train(sess, net, X, y, ckpt_path, logname, batch_size=16, epochs=10):
    losses = []
    accuracy = []
    it = trange(epochs)
    n_steps = int(np.ceil(len(X)/batch_size))
    writer = tf.summary.FileWriter(logname, sess.graph)
    for e in it:
        # Shuffle
        # p = np.random.permutation(len(X))
        # X, y = X[p], y[p]
        xy = list(zip(X, y))
        random.shuffle(xy)
        X, y = zip(*xy)

        # Iterate batches
        for i in tqdm(range(n_steps)):
            l = i*batch_size
            u = l + batch_size
            X_batch, y_batch = X[l:u], y[l:u]
            X_batch = net.preprocess(X_batch)
            _, err, acc, summary = sess.run(
                [net.train_op, net.loss_op, net.acc_op, net.summary],
                feed_dict={
                    keep_prob: 0.4,
                    net.X: X_batch,
                    net.y: y_batch
                }
            )
            losses.append(err)
            accuracy.append(acc)
            it.set_postfix(
                loss=np.mean(losses[-batch_size:]) if losses else None,
                acc=np.mean(accuracy[-batch_size:]) if accuracy else None)
            writer.add_summary(summary, e*n_steps+i)
        saver.save(sess, ckpt_path)
    return losses


class RolloutPolicyNet:
    def __init__(self, n_rules, fp_dim=8912, k=10):
        self.fp_dim = fp_dim
        self.n_rules = n_rules
        self.X = tf.placeholder(tf.float32, shape=(None, fp_dim), name='X')
        self.y = tf.placeholder(tf.int64, shape=(None,), name='y')

        inp = tf.math.log(self.X+1)
        net = tf.layers.dense(inp, 512, activation=tf.nn.elu)
        net = tf.nn.dropout(net, keep_prob=keep_prob)
        net = tf.layers.dense(net, n_rules, activation=None)
        self.pred_op = tf.argmax(tf.nn.softmax(net), 1)
        self.loss_op = tf.losses.sparse_softmax_cross_entropy(self.y, net)
        self.train_op = tf.train.AdamOptimizer(learning_rate=1e-4).minimize(self.loss_op)

        correct_pred = tf.equal(self.pred_op, self.y)
        self.acc_op = tf.reduce_mean(tf.cast(correct_pred, tf.float32))

        lsum = tf.summary.scalar('loss', self.loss_op)
        asum = tf.summary.scalar('accuracy', self.acc_op)
        self.summary = tf.summary.merge([lsum, asum])

    def preprocess(self, X):
        # Compute fingerprints
        return fps_to_arr(fingerprint_mols(X, self.fp_dim))


class ExpansionPolicyNet:
    def __init__(self, n_rules, fp_dim=1e4):
        self.fp_dim = fp_dim
        self.n_rules = n_rules

        self.X = tf.placeholder(tf.float32, shape=(None, fp_dim))
        self.y = tf.placeholder(tf.int64, shape=(None,))
        self.k = tf.placeholder(tf.int32, shape=())

        # inp = self.X
        inp = tf.math.log(self.X+1)
        net = tf.layers.dense(inp, 512, activation=tf.nn.elu)
        net = tf.nn.dropout(net, keep_prob=keep_prob)
        for _ in range(5):
            net = highway_layer(net, activation=tf.nn.elu)
            net = tf.nn.dropout(net, keep_prob=keep_prob)

        net = tf.layers.dense(net, n_rules, activation=None)
        pred = tf.nn.softmax(net)
        self.pred = tf.nn.top_k(pred, k=self.k)
        self.loss_op = tf.losses.sparse_softmax_cross_entropy(self.y, net)
        self.train_op = tf.train.AdamOptimizer(learning_rate=1e-5).minimize(self.loss_op)

        correct_pred = tf.equal(tf.argmax(pred, 1), self.y)
        self.acc_op = tf.reduce_mean(tf.cast(correct_pred, tf.float32))

        tf.summary.scalar('loss', self.loss_op)
        tf.summary.scalar('accuracy', self.acc_op)
        self.summary = tf.summary.merge_all()

    def preprocess(self, X):
        # Compute fingerprints
        X = fingerprint_mols(X, self.fp_dim)
        return fps_to_arr(X)


class InScopeFilterNet:
    def __init__(self, product_fp_dim=16384, reaction_fp_dim=2048):
        self.prod_fp_dim = product_fp_dim
        self.react_fp_dim = reaction_fp_dim
        self.X = tf.placeholder(tf.float32, shape=(None, product_fp_dim+reaction_fp_dim), name='X')
        self.X_prod = self.X[:,:product_fp_dim]
        self.X_react = self.X[:,product_fp_dim:]
        self.y = tf.placeholder(tf.int32, shape=(None,), name='y')

        # Product branch
        prod_inp = tf.math.log(self.X_prod+1)
        prod_net = tf.layers.dense(prod_inp, 1024, activation=tf.nn.elu)
        prod_net = tf.nn.dropout(prod_net, keep_prob=keep_prob)
        for _ in range(5):
            prod_net = highway_layer(prod_net, activation=tf.nn.elu)

        # Reaction branch
        react_net = tf.layers.dense(self.X_react, 1024, activation=tf.nn.elu)

        # Cosine similarity
        prod_norm = tf.nn.l2_normalize(prod_net, axis=-1)
        react_norm = tf.nn.l2_normalize(react_net, axis=-1)
        cosine_sim = tf.reduce_sum(tf.multiply(prod_norm, react_norm), axis=-1)

        # Paper's architecture passes the similarity through a sigmoid function
        # but that seems redundant?
        self.pred = tf.nn.sigmoid(cosine_sim)

        self.loss_op = tf.losses.log_loss(self.y, self.pred)
        self.train_op = tf.train.AdamOptimizer(learning_rate=0.001).minimize(self.loss_op)

        correct_pred = tf.equal(self.y, tf.cast(tf.round(self.pred), tf.int32))
        self.acc_op = tf.reduce_mean(tf.cast(correct_pred, tf.float32))

        lsum = tf.summary.scalar('loss', self.loss_op)
        asum = tf.summary.scalar('accuracy', self.acc_op)
        self.summary = tf.summary.merge([lsum, asum])

    def preprocess(self, X):
        # Compute fingerprints
        prod_mols, react_mols = zip(*X)
        prod_fps = fingerprint_mols(prod_mols, self.prod_fp_dim)
        react_fps = fingerprint_reactions(react_mols, self.react_fp_dim)
        return np.hstack([prod_fps, react_fps])


if __name__ == '__main__':
    import json
    from collections import defaultdict

    prod_to_rules = defaultdict(set)
    with open('data/templates.dat', 'r') as f:
        for l in tqdm(f, desc='Loading reaction templates'):
            rule, prod = l.strip().split('\t')
            prod_to_rules[prod].add(rule)

    rollout_rules = {}
    with open('data/rollout.dat', 'r') as f:
        for i, l in tqdm(enumerate(f), desc='Loading rollout rules'):
            rule = l.strip()
            rollout_rules[rule] = i

    expansion_rules = {}
    with open('data/expansion.dat', 'r') as f:
        for i, l in tqdm(enumerate(f), desc='Loading expansion rules'):
            rule = l.strip()
            expansion_rules[rule] = i

    rule_groups = defaultdict(list)
    for prod, rules in prod_to_rules.items():
        rules = [r for r in rules if r in expansion_rules]
        if not rules: continue
        for r in rules:
            rule_groups[r].append(prod)

    all_sims = []
    from scipy.spatial import distance
    for rule, prods in tqdm(rule_groups.items()):
       fprs = fingerprint_mols(prods, 1e4)
       sims = []
       for i, f in enumerate(fprs):
           for j, k in enumerate(fprs):
               if i == j: continue
               sim = 1 - distance.euclidean(f, k)
               sims.append(sim)
       if not sims:
            continue
       all_sims.append(np.mean(sims))

    notsims = []
    rules = list(rule_groups.keys())
    while len(notsims) < 10000:
        a = random.choice(rules)
        b = random.choice(rules)
        if a == b: continue
        p_a = random.choice(rule_groups[a])
        p_b = random.choice(rule_groups[b])
        fprs = fingerprint_mols([p_a, p_b], 1e4)
        sim = 1 - distance.euclidean(fprs[0], fprs[1])
        notsims.append(sim)
    # import ipdb; ipdb.set_trace()

    # Build models
    with tf.variable_scope('rollout'):
        rollout = RolloutPolicyNet(n_rules=len(rollout_rules))

    with tf.variable_scope('filter'):
        filter = InScopeFilterNet()

    # Save metadata and weights
    save_path = 'model'
    ckpt_path = os.path.join(save_path, 'model.ckpt')
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    with open(os.path.join(save_path, 'rules.json'), 'w') as f:
        json.dump({
            'rollout': rollout_rules,
            'expansion': expansion_rules
        }, f)

    sess = tf.Session()
    init = tf.global_variables_initializer()
    sess.run(init)
    saver = tf.train.Saver()
    saver.restore(sess, ckpt_path)

    # Rollout training
    print('Rollout training...')
    X, y = [], []
    for prod, rules in tqdm(prod_to_rules.items(), desc='data prep'):
        rules = [r for r in rules if r in rollout_rules]
        if not rules: continue

        # Ideally trained as multilabel,
        # but multiclass, single label is easier atm
        for r in rules:
            id = rollout_rules[r]
            y.append(id)
            X.append(prod)
    print('Training size:', len(X))
    # train(sess, rollout, X, y, ckpt_path, '/tmp/log/rollout', batch_size=256, epochs=250)

    # Check
    # X = rollout.preprocess(X[20:30])
    # y_pred = sess.run(rollout.pred_op, feed_dict={
    #     keep_prob: 1.,
    #     rollout.X: X
    # })
    # print(list(np.argmax(y_pred, 1)))
    # print(y[20:30])

    print('In-Scope Filter training...')
    X, y = [], []
    exists = set()
    for prod, rules in tqdm(prod_to_rules.items(), desc='data prep'):
        rules = [r for r in rules if r in expansion_rules]
        if not rules: continue

        for r in rules:
            y.append(1.)
            X.append((prod, r))
            exists.add('{}_{}'.format(prod, r))

    # Generate negative examples
    target_size = len(X) * 2
    pbar = tqdm(total=target_size//2, desc='data prep (negative)')
    prods = list(prod_to_rules.keys())
    exprules = list(expansion_rules.keys())
    while len(X) < target_size:
        prod = random.choice(prods)
        rule = random.choice(exprules)

        key = '{}_{}'.format(prod, r)
        if key in exists:
            continue
        else:
            y.append(0.)
            X.append((prod, rule))
            pbar.update(1)
    pbar.close()
    print('Training size:', len(X))
    train(sess, filter, X, y, ckpt_path, '/tmp/log/inscopefilter', batch_size=512, epochs=3)

    # for v in tf.trainable_variables():
    #     print(v)