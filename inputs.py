import os

import tensorflow as tf

# Expects .tfrecords files as produced by the script in datasets in a google storage bucket

def generic_text(params, eval=False):
    # params["datasets"] = [(train glob, eval_glob, stitch, ["random_sample", "sample", "chunk"] weight)]
    i = 0 if not eval else 1
    datasets = [text_dataset(tf.io.gfile.glob(os.path.join(params["data_path"], dataset[i])), 
                params, stitch=dataset[2], datatype=dataset[3], batch=False)
                for dataset in params["datasets"]]
    weights = [dataset[4] for dataset in params["datasets"]]

    dataset = tf.data.experimental.sample_from_datasets(datasets, weights=weights)
    dataset = dataset.batch(params["batch_size"], drop_remainder=True).prefetch(params["iterations"] * 2)

    return dataset

def text_dataset(files, params, stitch, datatype, batch=True):
    dataset = tf.data.Dataset.from_tensor_slices(files)
    dataset = dataset.apply(tf.data.experimental.parallel_interleave(tf.data.TFRecordDataset, cycle_length=4, sloppy=False))

    if "sample" in datatype:
        def _parse_function(example_proto):
            features = {
                "hash": tf.VarLenFeature(tf.string),
                "text": tf.VarLenFeature(tf.int64)
            }
            parsed_features = tf.parse_single_example(example_proto, features)
            return parsed_features["text"], parsed_features["text"].dense_shape[0]
    else:
        def _parse_function(example_proto):
            features = {
                "text": tf.VarLenFeature(tf.int64)
            }
            parsed_features = tf.parse_single_example(example_proto, features)
            return parsed_features["text"] # Assuming the text is not sparse

    dataset = dataset.map(_parse_function, num_parallel_calls=1)

    # Subsample method
    if "sample" in datatype:
        # Since samples can be less than the correct length, and TPUs don't like variable lengths, this function stitches together enough samples
        # to have a text at least 1024 tokens long. For this to work the stitch parameter must be correctly tuned so that
        # stitch * min(characters_in_text) >= amount
        def _stitch_text(x, y):
            x = tf.sparse.to_dense(x)

            def _get_x(i):
                return tf.gather(x[i], tf.range(y[i]))

            out = _get_x(0)
            for i in range(1, stitch):
                out = tf.concat([out, [50256], _get_x(i)], axis=0) # text1<|endoftext|>text2

            return out

        # Hack-y way to stitch together multiple texts
        dataset = dataset.shuffle(1000 * stitch).batch(stitch, drop_remainder=True).map(_stitch_text, num_parallel_calls=tf.data.experimental.AUTOTUNE)

        # Sample 1024(+1) tokens from the stitched together text
        if datatype == "random_sample":
            def _sample_text(x):
                s = tf.size(x)
                r = tf.random.uniform([], maxval=s-(params["n_ctx"]+1), dtype=tf.dtypes.int32)
                r1 = tf.range(r, r+params["n_ctx"])
                r2 = tf.range(r+1, (r+1)+params["n_ctx"])
                r1 = tf.reshape(r1, [params["n_ctx"]]) # Somehow, this makes the compiler happy
                r2 = tf.reshape(r2, [params["n_ctx"]]) # TPUs want constant sized input, and these reshapes makes it recognize the shape of the input
                vals1 = tf.gather(x, r1)
                vals2 = tf.gather(x, r2)

                vals1 = tf.reshape(vals1, [params["n_ctx"]])
                vals2 = tf.reshape(vals2, [params["n_ctx"]])
                return vals1, vals2

        else:
            def _sample_text(x):
                vals1 = x[:params["n_ctx"]]
                vals2 = x[1:params["n_ctx"]+1]

                vals1 = tf.reshape(vals1, [params["n_ctx"]])
                vals2 = tf.reshape(vals2, [params["n_ctx"]])
                return vals1, vals2

        dataset = dataset.map(_sample_text, num_parallel_calls=tf.data.experimental.AUTOTUNE)
        
    if batch:
        dataset = dataset.batch(params["batch_size"], drop_remainder=True).prefetch(params["iterations"] * 2)

    dataset = dataset.repeat()

    return dataset

# Create a batch of text
def gpt2_pred_input(params, text=None):
    from models.gpt2 import encoder
    enc = encoder.get_encoder(params["encoder_path"])
    tokens = enc.encode(text)
    if len(tokens) > 1024:
        tokens = tokens[:1024]
    t = tf.broadcast_to(tokens, [params["batch_size"], len(tokens)])
    dataset = tf.data.Dataset.from_tensors(t)
    return dataset